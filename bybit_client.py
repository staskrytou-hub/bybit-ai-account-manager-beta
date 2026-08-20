from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import threading
from typing import Any
from urllib import parse, request, error

from trading_config import load_bybit_env


class BybitAPIError(RuntimeError):
    pass


class BybitClient:
    # Bybit requires authenticated timestamps to stay inside a narrow server-time window.
    # Windows clocks can occasionally drift by several seconds, so Stan keeps a measured
    # Bybit server offset and signs private requests with corrected time.
    _clock_offsets_ms: dict[str, float] = {}
    _clock_synced_at: dict[str, float] = {}
    _clock_sync_lock = threading.RLock()
    _clock_sync_ttl_s = 300.0

    def __init__(
        self,
        *,
        testnet: bool = False,
        authenticated: bool = False,
        timeout: int = 15,
        api_key: str = "",
        api_secret: str = "",
    ) -> None:
        self.testnet = bool(testnet)
        self.authenticated = bool(authenticated)
        self.timeout = int(timeout)
        self.base_url = "https://api-testnet.bybit.com" if self.testnet else "https://api.bybit.com"

        explicit_key = (api_key or "").strip()
        explicit_secret = (api_secret or "").strip()
        if explicit_key or explicit_secret:
            # Used by the connection wizard to verify a candidate key before it is saved.
            self.api_key = explicit_key
            self.api_secret = explicit_secret
        elif self.authenticated:
            load_bybit_env()
            self.api_key = os.getenv("BYBIT_API_KEY", "").strip()
            self.api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
        else:
            # Public market-data requests do not need to touch local credentials.
            self.api_key = ""
            self.api_secret = ""

        if self.authenticated and not (self.api_key and self.api_secret):
            raise BybitAPIError("Bybit API credentials are not configured.")

    @staticmethod
    def _extract_server_time_ms(result: dict[str, Any]) -> float:
        try:
            value = float(result.get("time") or 0)
            if value > 0:
                return value
        except Exception:
            pass
        data = result.get("result") or {}
        try:
            nano = float(data.get("timeNano") or 0)
            if nano > 0:
                return nano / 1_000_000.0
        except Exception:
            pass
        try:
            sec = float(data.get("timeSecond") or 0)
            if sec > 0:
                return sec * 1000.0
        except Exception:
            pass
        return 0.0

    def _sync_server_clock(self, *, force: bool = False) -> float:
        key = self.base_url
        now_mono = time.monotonic()
        with self._clock_sync_lock:
            cached_at = float(self._clock_synced_at.get(key, 0.0) or 0.0)
            if not force and key in self._clock_offsets_ms and (now_mono - cached_at) < self._clock_sync_ttl_s:
                return float(self._clock_offsets_ms[key])

            local_before = time.time() * 1000.0
            result = self._request("GET", "/v5/market/time", _retry_time_sync=False, _skip_time_sync=True)
            local_after = time.time() * 1000.0
            server_ms = self._extract_server_time_ms(result)
            if server_ms <= 0:
                raise BybitAPIError("Bybit server time synchronization returned no usable timestamp.")
            midpoint = (local_before + local_after) / 2.0
            offset = server_ms - midpoint
            self._clock_offsets_ms[key] = offset
            self._clock_synced_at[key] = time.monotonic()
            return offset

    def _corrected_timestamp_ms(self) -> int:
        offset = self._sync_server_clock(force=False)
        return int(time.time() * 1000.0 + offset)

    def get_clock_sync_status(self) -> dict[str, Any]:
        offset = self._sync_server_clock(force=False)
        return {
            "base_url": self.base_url,
            "offset_ms": round(float(offset), 1),
            "corrected_timestamp_ms": self._corrected_timestamp_ms(),
            "synced": True,
        }

    @staticmethod
    def _canonical_query(params: dict[str, Any] | None) -> str:
        clean = []
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            clean.append((str(key), str(value)))
        return parse.urlencode(clean)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
        _retry_time_sync: bool = True,
        _skip_time_sync: bool = False,
    ) -> dict[str, Any]:
        method = method.upper()
        query = self._canonical_query(params)
        url = self.base_url + path + (("?" + query) if query else "")
        payload: bytes | None = None
        if body is not None:
            body_string = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            payload = body_string.encode("utf-8")
        else:
            body_string = ""

        headers = {"Content-Type": "application/json", "User-Agent": "BybitAIAccountManager/4.6.9"}
        if private:
            if not self.api_key or not self.api_secret:
                raise BybitAPIError("Bybit API credentials are not configured.")
            if _skip_time_sync:
                timestamp_ms = int(time.time() * 1000.0)
            else:
                timestamp_ms = self._corrected_timestamp_ms()
            timestamp = str(timestamp_ms)
            recv_window = "5000"
            sign_payload = timestamp + self.api_key + recv_window + (query if method == "GET" else body_string)
            signature = hmac.new(self.api_secret.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": signature,
            })

        req = request.Request(url, data=payload, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                detail = str(exc)
            raise BybitAPIError(f"Bybit HTTP {exc.code}: {detail}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise BybitAPIError(f"Bybit network error: {exc}") from exc

        if not isinstance(result, dict):
            raise BybitAPIError("Bybit returned an unexpected response.")
        code = int(result.get("retCode", 0) or 0)
        if code == 10002 and private and _retry_time_sync:
            # A clock jump, sleep/resume, Windows drift or long scheduling pause can invalidate
            # the cached offset. Force a fresh Bybit-time measurement, re-sign and retry once.
            self._sync_server_clock(force=True)
            return self._request(
                method, path, params=params, body=body, private=private,
                _retry_time_sync=False, _skip_time_sync=False,
            )
        if code != 0:
            raise BybitAPIError(f"Bybit retCode={code}: {result.get('retMsg','Unknown error')}")
        return result

    def get_server_time(self) -> dict[str, Any]:
        return self._request("GET", "/v5/market/time")

    def get_kline(self, symbol: str, interval: str = "15", limit: int = 200, category: str = "linear") -> list[list[str]]:
        result = self._request("GET", "/v5/market/kline", params={"category": category, "symbol": symbol.upper(), "interval": interval, "limit": min(max(limit, 2), 1000)})
        return list((result.get("result") or {}).get("list") or [])


    def get_tickers(self, category: str = "linear") -> list[dict[str, Any]]:
        result = self._request("GET", "/v5/market/tickers", params={"category": category})
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]


    def get_announcements(self, *, locale: str = "en-US", announcement_type: str = "latest_activities", limit: int = 50, page: int = 1) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"locale": locale, "limit": max(1, min(int(limit), 50)), "page": max(1, int(page))}
        if announcement_type:
            params["type"] = announcement_type
        result = self._request("GET", "/v5/announcements/index", params=params)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_instruments(self, category: str = "spot") -> list[dict[str, Any]]:
        result = self._request("GET", "/v5/market/instruments-info", params={"category": category})
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_earn_products(self, *, category: str = "FlexibleSaving", coin: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if coin:
            params["coin"] = coin.upper()
        result = self._request("GET", "/v5/earn/product", params=params)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_fixed_earn_products(self, *, coin: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if coin:
            params["coin"] = coin.upper()
        result = self._request("GET", "/v5/earn/fixed-term/product", params=params)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_unified_wallet(self, coin: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"accountType": "UNIFIED"}
        if coin:
            params["coin"] = coin.upper()
        result = self._request("GET", "/v5/account/wallet-balance", params=params, private=True)
        return dict(result.get("result") or {})

    def get_api_key_info(self) -> dict[str, Any]:
        result = self._request("GET", "/v5/user/query-api", private=True)
        return dict(result.get("result") or {})

    def get_fee_rate(self, symbol: str = "", category: str = "linear") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol.upper()
        result = self._request("GET", "/v5/account/fee-rate", params=params, private=True)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_closed_pnl(self, symbol: str = "", category: str = "linear", limit: int = 100, start_time_ms: int | None = None, end_time_ms: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "limit": max(1, min(int(limit), 200))}
        if symbol:
            params["symbol"] = symbol.upper()
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        result = self._request("GET", "/v5/position/closed-pnl", params=params, private=True)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_open_orders(self, symbol: str = "", category: str = "linear", limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "limit": max(1, min(int(limit), 50)), "openOnly": 0}
        if symbol:
            params["symbol"] = symbol.upper()
        else:
            params["settleCoin"] = "USDT"
        result = self._request("GET", "/v5/order/realtime", params=params, private=True)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_order_realtime(self, *, symbol: str = "", order_id: str = "", order_link_id: str = "", category: str = "linear") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "limit": 20}
        if order_id:
            params["orderId"] = order_id
        elif order_link_id:
            params["orderLinkId"] = order_link_id
        elif symbol:
            params["symbol"] = symbol.upper()
        else:
            params["settleCoin"] = "USDT"
        result = self._request("GET", "/v5/order/realtime", params=params, private=True)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_order_history(self, *, symbol: str = "", order_id: str = "", order_link_id: str = "", category: str = "linear", limit: int = 20) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "limit": max(1, min(int(limit), 50))}
        if order_id:
            params["orderId"] = order_id
        elif order_link_id:
            params["orderLinkId"] = order_link_id
        elif symbol:
            params["symbol"] = symbol.upper()
        result = self._request("GET", "/v5/order/history", params=params, private=True)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_executions(self, symbol: str = "", category: str = "linear", limit: int = 100, order_id: str = "", order_link_id: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "limit": max(1, min(int(limit), 100))}
        if order_id:
            params["orderId"] = order_id
        elif order_link_id:
            params["orderLinkId"] = order_link_id
        elif symbol:
            params["symbol"] = symbol.upper()
        result = self._request("GET", "/v5/execution/list", params=params, private=True)
        return [dict(x) for x in list((result.get("result") or {}).get("list") or [])]

    def get_kline_history(self, symbol: str, interval: str = "15", candles: int = 1000, category: str = "linear") -> list[list[str]]:
        """Fetch up to several thousand historical klines by paging backward. Returns newest-first rows like Bybit."""
        target = max(2, min(int(candles), 5000))
        all_rows: list[list[str]] = []
        end: int | None = None
        seen: set[str] = set()
        while len(all_rows) < target:
            limit = min(1000, target - len(all_rows))
            params: dict[str, Any] = {"category": category, "symbol": symbol.upper(), "interval": interval, "limit": limit}
            if end is not None:
                params["end"] = end
            result = self._request("GET", "/v5/market/kline", params=params)
            rows = list((result.get("result") or {}).get("list") or [])
            if not rows:
                break
            added = 0
            oldest: int | None = None
            for row in rows:
                if not row:
                    continue
                key = str(row[0])
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append(row)
                added += 1
                try:
                    ts = int(float(row[0]))
                    oldest = ts if oldest is None else min(oldest, ts)
                except Exception:
                    pass
            if added == 0 or oldest is None or len(rows) < limit:
                break
            end = oldest - 1
            time.sleep(0.03)
        all_rows.sort(key=lambda r: int(float(r[0])) if r else 0, reverse=True)
        return all_rows[:target]

    def get_ticker(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        result = self._request("GET", "/v5/market/tickers", params={"category": category, "symbol": symbol.upper()})
        items = list((result.get("result") or {}).get("list") or [])
        return dict(items[0]) if items else {}

    def get_orderbook(self, symbol: str, category: str = "linear", limit: int = 25) -> dict[str, Any]:
        result = self._request("GET", "/v5/market/orderbook", params={"category": category, "symbol": symbol.upper(), "limit": limit})
        return dict(result.get("result") or {})

    def get_open_interest(self, symbol: str, interval_time: str = "15min", category: str = "linear", limit: int = 10) -> list[dict[str, Any]]:
        result = self._request("GET", "/v5/market/open-interest", params={"category": category, "symbol": symbol.upper(), "intervalTime": interval_time, "limit": limit})
        return list((result.get("result") or {}).get("list") or [])

    def get_long_short_ratio(self, symbol: str, period: str = "15min", category: str = "linear", limit: int = 10) -> list[dict[str, Any]]:
        result = self._request("GET", "/v5/market/account-ratio", params={"category": category, "symbol": symbol.upper(), "period": period, "limit": limit})
        return list((result.get("result") or {}).get("list") or [])

    def get_funding_history(self, symbol: str, category: str = "linear", limit: int = 5) -> list[dict[str, Any]]:
        result = self._request("GET", "/v5/market/funding/history", params={"category": category, "symbol": symbol.upper(), "limit": limit})
        return list((result.get("result") or {}).get("list") or [])

    def get_instrument(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        result = self._request("GET", "/v5/market/instruments-info", params={"category": category, "symbol": symbol.upper()})
        items = list((result.get("result") or {}).get("list") or [])
        return dict(items[0]) if items else {}

    def get_wallet_balance(self, coin: str = "USDT") -> dict[str, Any]:
        return self.get_unified_wallet(coin)

    def get_positions(self, symbol: str = "", category: str = "linear", settle_coin: str = "USDT") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if symbol: params["symbol"] = symbol.upper()
        else: params["settleCoin"] = settle_coin.upper()
        result = self._request("GET", "/v5/position/list", params=params, private=True)
        return list((result.get("result") or {}).get("list") or [])

    def switch_position_mode(self, *, symbol: str = "", coin: str = "", mode: int = 0, category: str = "linear") -> dict[str, Any]:
        body: dict[str, Any] = {"category": category, "mode": int(mode)}
        if symbol:
            body["symbol"] = symbol.upper()
        elif coin:
            body["coin"] = coin.upper()
        else:
            raise ValueError("symbol or coin is required to switch Bybit position mode")
        return self._request("POST", "/v5/position/switch-mode", body=body, private=True)

    def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> dict[str, Any]:
        lev = str(max(1.0, float(leverage))).rstrip("0").rstrip(".")
        return self._request("POST", "/v5/position/set-leverage", body={"category": category, "symbol": symbol.upper(), "buyLeverage": lev, "sellLeverage": lev}, private=True)

    def set_trading_stop(self, *, symbol: str, stop_loss: str, take_profit: str, category: str = "linear", position_idx: int = 0) -> dict[str, Any]:
        body = {
            "category": category,
            "symbol": symbol.upper(),
            "tpslMode": "Full",
            "positionIdx": int(position_idx),
            "stopLoss": str(stop_loss),
            "takeProfit": str(take_profit),
            "slTriggerBy": "MarkPrice",
            "tpTriggerBy": "MarkPrice",
        }
        return self._request("POST", "/v5/position/trading-stop", body=body, private=True)

    def place_order(self, *, symbol: str, side: str, qty: str, order_type: str = "Market", price: str = "", stop_loss: str = "", take_profit: str = "", category: str = "linear", reduce_only: bool = False, order_link_id: str = "", slippage_tolerance_pct: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "category": category,
            "symbol": symbol.upper(),
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "timeInForce": "GTC",
            "positionIdx": 0,
            "reduceOnly": bool(reduce_only),
        }
        if price and order_type == "Limit": body["price"] = price
        if order_type == "Market" and slippage_tolerance_pct is not None:
            pct = max(0.01, min(float(slippage_tolerance_pct), 10.0))
            body["slippageToleranceType"] = "Percent"
            body["slippageTolerance"] = f"{pct:.2f}"
        if stop_loss and not reduce_only: body["stopLoss"] = stop_loss
        if take_profit and not reduce_only: body["takeProfit"] = take_profit
        if order_link_id: body["orderLinkId"] = order_link_id[:36]
        return self._request("POST", "/v5/order/create", body=body, private=True)

    def place_spot_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: str,
        order_type: str = "Limit",
        price: str = "",
        time_in_force: str = "GTC",
        order_link_id: str = "",
        take_profit: str = "",
        stop_loss: str = "",
        tp_order_type: str = "Market",
        sl_order_type: str = "Market",
        market_unit: str = "baseCoin",
        slippage_tolerance_pct: float | None = None,
    ) -> dict[str, Any]:
        """Place a non-margin Spot order. Futures-only fields are deliberately excluded."""
        body: dict[str, Any] = {
            "category": "spot",
            "symbol": symbol.upper(),
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": time_in_force,
            "isLeverage": 0,
            "orderFilter": "Order",
        }
        if order_type == "Limit":
            if not price:
                raise ValueError("price is required for Spot Limit orders")
            body["price"] = str(price)
            # Bybit supports attached TP/SL on Spot Limit orders.
            if take_profit:
                body["takeProfit"] = str(take_profit)
                body["tpOrderType"] = tp_order_type
            if stop_loss:
                body["stopLoss"] = str(stop_loss)
                body["slOrderType"] = sl_order_type
        else:
            body["marketUnit"] = market_unit
            if slippage_tolerance_pct is not None:
                pct = max(0.01, min(float(slippage_tolerance_pct), 10.0))
                body["slippageToleranceType"] = "Percent"
                body["slippageTolerance"] = f"{pct:.2f}"
        if order_link_id:
            body["orderLinkId"] = order_link_id[:36]
        return self._request("POST", "/v5/order/create", body=body, private=True)

    def cancel_order(self, *, symbol: str, order_id: str = "", order_link_id: str = "", category: str = "spot") -> dict[str, Any]:
        if not order_id and not order_link_id:
            raise ValueError("order_id or order_link_id is required")
        body: dict[str, Any] = {"category": category, "symbol": symbol.upper()}
        if order_id:
            body["orderId"] = order_id
        else:
            body["orderLinkId"] = order_link_id[:36]
        return self._request("POST", "/v5/order/cancel", body=body, private=True)

