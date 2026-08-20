from __future__ import annotations

from typing import Any
import time

from bybit_client import BybitClient, BybitAPIError




def _server_time_ms(server: dict[str, Any]) -> float:
    try:
        value = float(server.get("time") or 0)
        if value > 0:
            return value
    except Exception:
        pass
    result = (server.get("result") or {}) if isinstance(server, dict) else {}
    try:
        nano = float(result.get("timeNano") or 0)
        if nano > 0:
            return nano / 1_000_000.0
    except Exception:
        pass
    try:
        sec = float(result.get("timeSecond") or 0)
        return sec * 1000.0 if sec > 0 else 0.0
    except Exception:
        return 0.0

def _inspect(testnet: bool, api_key: str = "", api_secret: str = "") -> dict[str, Any]:
    client = BybitClient(
        testnet=testnet,
        authenticated=True,
        api_key=api_key,
        api_secret=api_secret,
    )
    info = client.get_api_key_info()
    permissions = info.get("permissions") or {}
    contract = set(str(x) for x in (permissions.get("ContractTrade") or []))
    spot = set(str(x) for x in (permissions.get("Spot") or []))
    earn = set(str(x) for x in (permissions.get("Earn") or []))
    options = set(str(x) for x in (permissions.get("Options") or []))
    wallet = set(str(x) for x in (permissions.get("Wallet") or []))
    read_only = int(info.get("readOnly", -1) or 0) == 1
    can_trade = (not read_only) and {"Order", "Position"}.issubset(contract)
    can_trade_spot = (not read_only) and "SpotTrade" in spot
    can_use_earn = (not read_only) and "Earn" in earn
    unsafe_wallet = bool(wallet)

    equity = 0.0
    open_positions = 0
    clock_drift_ms = None
    clock_offset_ms = None
    clock_corrected_drift_ms = None
    fee_rate = None
    try:
        wallet_data = client.get_wallet_balance("USDT")
        items = list(wallet_data.get("list") or [])
        if items:
            row = items[0]
            for k in ("totalEquity", "totalWalletBalance"):
                try:
                    value = float(row.get(k) or 0)
                    if value > 0:
                        equity = value
                        break
                except Exception:
                    pass
    except Exception:
        pass
    try:
        positions = client.get_positions(settle_coin="USDT")
        open_positions = len([p for p in positions if float(p.get("size") or 0) > 0])
    except Exception:
        pass
    try:
        server = client.get_server_time()
        server_ms = _server_time_ms(server)
        local_ms = time.time() * 1000.0
        if server_ms > 0:
            clock_drift_ms = abs(server_ms - local_ms)
        if hasattr(client, "get_clock_sync_status"):
            sync = client.get_clock_sync_status()
            clock_offset_ms = float(sync.get("offset_ms") or 0.0)
        else:
            clock_offset_ms = server_ms - local_ms if server_ms else 0.0
        if server_ms > 0:
            clock_corrected_drift_ms = abs(server_ms - (local_ms + clock_offset_ms))
    except Exception:
        pass
    try:
        fees = client.get_fee_rate(category="linear")
        if fees:
            fee_rate = fees[0].get("takerFeeRate")
    except Exception:
        pass

    return {
        "ok": True,
        "environment": "testnet" if testnet else "mainnet",
        "testnet": testnet,
        "read_only": read_only,
        "can_trade_contracts": can_trade,
        "can_trade_spot": can_trade_spot,
        "can_use_earn": can_use_earn,
        "can_trade_options": (not read_only) and bool(options),
        "unsafe_wallet_permissions": unsafe_wallet,
        "permissions": permissions,
        "capabilities": {
            "futures_trade": can_trade,
            "spot_trade": can_trade_spot,
            "earn": can_use_earn,
            "options": (not read_only) and bool(options),
            "wallet_transfer": bool(wallet),
        },
        "ips": info.get("ips") or [],
        "note": str(info.get("note", "")),
        "key_type": info.get("type"),
        "key_id": str(info.get("id", "")),
        "equity_usdt": round(equity, 6),
        "open_positions": open_positions,
        "clock_drift_ms": None if clock_drift_ms is None else round(clock_drift_ms, 1),
        "clock_offset_ms": None if clock_offset_ms is None else round(clock_offset_ms, 1),
        "clock_corrected_drift_ms": None if clock_corrected_drift_ms is None else round(clock_corrected_drift_ms, 1),
        "taker_fee_rate": fee_rate,
    }


def detect_bybit_key_environment() -> dict[str, Any]:
    """Detect whether the saved key belongs to mainnet or testnet without exposing secrets."""
    errors: list[str] = []
    for testnet in (False, True):
        try:
            return _inspect(testnet)
        except Exception as exc:
            errors.append(f"{'testnet' if testnet else 'mainnet'}: {type(exc).__name__}: {exc}")
    raise BybitAPIError("Saved Bybit credentials did not authenticate on mainnet or testnet. " + " | ".join(errors))



def detect_candidate_key_environment(api_key: str, api_secret: str) -> dict[str, Any]:
    """Validate an unsaved candidate key without persisting or logging the secret."""
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    if not key or not secret:
        raise BybitAPIError("Enter both the Bybit API key and API secret.")
    errors: list[str] = []
    for testnet in (False, True):
        try:
            return _inspect(testnet, key, secret)
        except Exception as exc:
            errors.append(f"{'testnet' if testnet else 'mainnet'}: {type(exc).__name__}: {exc}")
    raise BybitAPIError("The key did not authenticate on Bybit Mainnet or Testnet. " + " | ".join(errors))


def validate_candidate_credentials(api_key: str, api_secret: str) -> dict[str, Any]:
    """Return the autonomous mode implied by candidate credentials, before saving them."""
    result = detect_candidate_key_environment(api_key, api_secret)
    return _apply_autopilot_policy(result)


def _apply_autopilot_policy(result: dict[str, Any]) -> dict[str, Any]:
    if result["testnet"]:
        result["autopilot_mode"] = "testnet"
        result["key_environment"] = "testnet"
        result["live_armed"] = False
        result["message"] = "Bybit Testnet key detected. Stan can use autonomous Testnet execution after research bootstrap."
        return result

    if result["read_only"]:
        result["autopilot_mode"] = "shadow"
        result["key_environment"] = "mainnet_readonly"
        result["live_armed"] = False
        result["message"] = "Mainnet read-only key detected. Stan can analyze the account but cannot place orders."
        return result

    if result["unsafe_wallet_permissions"]:
        result["autopilot_mode"] = "shadow"
        result["key_environment"] = "mainnet_readonly"
        result["live_armed"] = False
        result["blocked_reason"] = "For safety, Stan refuses live Autopilot when the API key has Wallet permissions. Create a dedicated key with ContractTrade: Order + Position only."
        result["message"] = result["blocked_reason"]
        return result

    if not result["can_trade_contracts"]:
        result["autopilot_mode"] = "shadow"
        result["key_environment"] = "mainnet_readonly"
        result["live_armed"] = False
        result["blocked_reason"] = "The key needs ContractTrade Order + Position permissions for autonomous futures execution."
        result["message"] = result["blocked_reason"]
        return result

    result["autopilot_mode"] = "autopilot_live"
    result["key_environment"] = "mainnet_trade"
    result["live_armed"] = True
    result["message"] = "Dedicated Mainnet futures-trading key detected. Safe Learning Autopilot can be armed."
    return result

def validate_autopilot_key() -> dict[str, Any]:
    """Validate the saved key and return the safe autonomous mode implied by it."""
    return _apply_autopilot_policy(detect_bybit_key_environment())
