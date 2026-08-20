from __future__ import annotations

import math
from typing import Any

from bybit_client import BybitClient


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def unified_margin_state(client: BybitClient) -> dict[str, Any]:
    """Read account-wide UTA capacity from Bybit without mutating the account.

    For cross/portfolio Unified Trading Accounts, Bybit documents
    ``totalAvailableBalance`` as the derivatives available-balance source of truth.
    It already reflects margin consumed by currently open derivatives positions and orders,
    which makes it the right capacity input when Stan is carrying (for example) BTCUSDT
    while evaluating another symbol.
    """
    wallet = client.get_unified_wallet()
    rows = list(wallet.get("list") or []) if isinstance(wallet, dict) else []
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    coins = list(first.get("coin") or []) if isinstance(first, dict) else []
    usdt = next((x for x in coins if isinstance(x, dict) and str(x.get("coin", "")).upper() == "USDT"), {})
    total_available_raw = first.get("totalAvailableBalance")
    total_available = _f(total_available_raw)
    # Bybit documents totalAvailableBalance for UTA cross/portfolio margin. In isolated
    # margin the account-wide field can be unavailable, so fall back to the documented
    # USDT derivatives formula instead of guessing from equity. A literal "0" remains zero.
    available_source = "Bybit UTA totalAvailableBalance"
    if total_available_raw in (None, ""):
        total_available = max(
            0.0,
            _f(usdt.get("walletBalance"))
            - _f(usdt.get("totalPositionIM"))
            - _f(usdt.get("totalOrderIM"))
            - _f(usdt.get("locked"))
            - _f(usdt.get("bonus")),
        )
        available_source = "Bybit isolated-margin USDT walletBalance-positionIM-orderIM-locked-bonus"
    state = {
        "account_type": str(first.get("accountType") or ""),
        "total_equity_usd": _f(first.get("totalEquity")),
        "total_wallet_balance_usd": _f(first.get("totalWalletBalance")),
        "total_margin_balance_usd": _f(first.get("totalMarginBalance")),
        "total_available_balance_usd": total_available,
        "total_initial_margin_usd": _f(first.get("totalInitialMargin")),
        "total_maintenance_margin_usd": _f(first.get("totalMaintenanceMargin")),
        "total_perp_upl_usd": _f(first.get("totalPerpUPL")),
        "account_im_rate": _f(first.get("accountIMRate")),
        "account_mm_rate": _f(first.get("accountMMRate")),
        "usdt_wallet_balance": _f(usdt.get("walletBalance")),
        "usdt_equity": _f(usdt.get("equity")),
        "usdt_locked": _f(usdt.get("locked")),
        "usdt_order_im": _f(usdt.get("totalOrderIM")),
        "usdt_position_im": _f(usdt.get("totalPositionIM")),
        "usdt_unrealised_pnl": _f(usdt.get("unrealisedPnl")),
        "usdt_bonus": _f(usdt.get("bonus")),
    }
    equity = state["total_equity_usd"]
    available = state["total_available_balance_usd"]
    state["available_pct_of_equity"] = round(available / equity * 100.0, 4) if equity > 0 else 0.0
    state["source"] = available_source
    return state


def live_position_inventory(client: BybitClient) -> dict[str, Any]:
    """Inventory live USDT-settled Futures positions after START/restart.

    Positions live at Bybit, not inside Stan's process. A STOP/build/restart therefore must
    *adopt* what the exchange currently reports instead of assuming a flat portfolio. Stan
    does not close, resize, or pyramid these positions here. It only records them and marks
    missing exchange-side TP/SL protection so the normal portfolio guard can block new risk.
    """
    try:
        rows = [x for x in client.get_positions(settle_coin="USDT") if _f(x.get("size")) > 0]
    except Exception as exc:
        return {"count": 0, "positions": [], "symbols": [], "unprotected": [], "error": f"{type(exc).__name__}: {exc}"}
    positions: list[dict[str, Any]] = []
    unprotected: list[str] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        stop = _f(row.get("stopLoss"))
        tp = _f(row.get("takeProfit"))
        protected = stop > 0 and tp > 0
        if symbol and not protected:
            unprotected.append(symbol)
        positions.append({
            "symbol": symbol,
            "side": str(row.get("side") or ""),
            "size": _f(row.get("size")),
            "avg_price": _f(row.get("avgPrice")),
            "mark_price": _f(row.get("markPrice")),
            "position_value": _f(row.get("positionValue")),
            "leverage": _f(row.get("leverage")),
            "unrealised_pnl": _f(row.get("unrealisedPnl")),
            "stop_loss": stop,
            "take_profit": tp,
            "position_im": _f(row.get("positionIM")),
            "protected": protected,
        })
    return {
        "count": len(positions),
        "positions": positions,
        "symbols": [x["symbol"] for x in positions if x.get("symbol")],
        "unprotected": sorted(set(unprotected)),
        "source": "Bybit /v5/position/list",
    }


def futures_minimum_notional(instrument: dict[str, Any] | None, price: float) -> float:
    info = instrument if isinstance(instrument, dict) else {}
    lot = info.get("lotSizeFilter") if isinstance(info.get("lotSizeFilter"), dict) else {}
    min_notional = _f(lot.get("minNotionalValue"))
    min_qty = _f(lot.get("minOrderQty"))
    if min_qty > 0 and price > 0:
        min_notional = max(min_notional, min_qty * price)
    return max(0.0, min_notional)


def instrument_leverage_cap(instrument: dict[str, Any] | None, configured_cap: float) -> float:
    cap = max(1.0, float(configured_cap or 1.0))
    info = instrument if isinstance(instrument, dict) else {}
    lev = info.get("leverageFilter") if isinstance(info.get("leverageFilter"), dict) else {}
    exchange_cap = _f(lev.get("maxLeverage"), cap)
    return max(1.0, min(cap, exchange_cap if exchange_cap > 0 else cap))


def usable_margin_budget(
    available_balance_usd: float,
    *,
    utilization_pct: float,
    absolute_reserve_usdt: float,
) -> float:
    """Capacity Stan is allowed to consume for one *new* Futures entry.

    A fixed reserve plus utilization haircut intentionally leaves room for fees, mark-price
    movement and small account-state changes between sizing and the order reaching Bybit.
    """
    available = max(0.0, float(available_balance_usd or 0.0))
    reserve = max(0.0, float(absolute_reserve_usdt or 0.0))
    util = max(0.10, min(0.95, float(utilization_pct or 82.0) / 100.0))
    return max(0.0, available - reserve) * util


def minimum_available_balance_for_futures(
    instrument: dict[str, Any] | None,
    *,
    price: float,
    max_leverage: float,
    utilization_pct: float,
    absolute_reserve_usdt: float = 0.0,
) -> float:
    """Conservative raw available-balance threshold for the exchange minimum order."""
    minimum_notional = futures_minimum_notional(instrument, price)
    if minimum_notional <= 0:
        return 0.0
    lev = max(1.0, float(max_leverage or 1.0))
    util = max(0.10, min(0.95, float(utilization_pct or 82.0) / 100.0))
    return minimum_notional / lev / util + max(0.0, float(absolute_reserve_usdt or 0.0))


def pre_ai_capacity_gate(
    *,
    available_balance_usd: float,
    minimum_notional_usdt: float,
    leverage_cap: float,
    utilization_pct: float,
    reserve_usdt: float,
) -> dict[str, Any]:
    """Token-free gate used before AI when even the minimum order may not fit.

    This gate is intentionally permissive: if the *minimum* exchange order can fit at the
    maximum currently allowed leverage, AI may still review the proposal. The Risk Engine
    later shrinks the desired quantity to live capacity. We only suppress AI when no legal
    quantity can fit at all.
    """
    available = max(0.0, float(available_balance_usd or 0.0))
    min_notional = max(0.0, float(minimum_notional_usdt or 0.0))
    lev = max(1.0, float(leverage_cap or 1.0))
    budget = usable_margin_budget(
        available,
        utilization_pct=utilization_pct,
        absolute_reserve_usdt=reserve_usdt,
    )
    required_margin = min_notional / lev if min_notional > 0 else 0.0
    fits = min_notional <= 0 or required_margin <= budget + 1e-9
    return {
        "allowed": bool(fits),
        "available_balance_usd": round(available, 4),
        "usable_margin_budget_usd": round(budget, 4),
        "minimum_notional_usdt": round(min_notional, 4),
        "leverage_cap": round(lev, 4),
        "minimum_initial_margin_estimate_usd": round(required_margin, 4),
        "reserve_usdt": round(max(0.0, float(reserve_usdt or 0.0)), 4),
        "utilization_pct": round(float(utilization_pct or 82.0), 2),
        "reason": "" if fits else (
            f"minimum order needs ~{required_margin:.2f} USDT initial margin but only "
            f"~{budget:.2f} USDT of current Bybit available balance is usable"
        ),
    }


def floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step + 1e-12) * step


def recent_capacity_reject_gate(
    record: dict[str, Any] | None,
    *,
    symbol: str,
    current_available_balance_usd: float,
    now_ts: float,
    cooldown_minutes: float = 20.0,
    recovery_usdt: float = 3.0,
) -> dict[str, Any]:
    """Suppress repeated paid review after a fresh 110007 until capacity materially changes.

    This is intentionally symbol-local and short-lived. Other markets remain searchable; the
    same market is unlocked early if free balance recovers by the configured absolute amount
    or by 20%, whichever is easier to observe on a small account.
    """
    item = dict(record or {})
    if str(item.get("symbol") or "").upper() != str(symbol or "").upper():
        return {"blocked": False, "reason": ""}
    try:
        seen_ts = float(item.get("ts") or 0.0)
    except Exception:
        seen_ts = 0.0
    age = max(0.0, float(now_ts or 0.0) - seen_ts) if seen_ts > 0 else float("inf")
    cooldown_seconds = max(60.0, float(cooldown_minutes or 20.0) * 60.0)
    if age > cooldown_seconds:
        return {"blocked": False, "reason": "", "age_seconds": round(age, 1)}
    cap = item.get("capacity") if isinstance(item.get("capacity"), dict) else {}
    prior_available = max(0.0, _f(cap.get("total_available_balance_usd")))
    current_available = max(0.0, float(current_available_balance_usd or 0.0))
    recovery_abs = max(0.5, float(recovery_usdt or 3.0))
    recovered = current_available >= prior_available + recovery_abs
    if prior_available > 0:
        recovered = recovered or current_available >= prior_available * 1.20
    if recovered:
        return {
            "blocked": False, "reason": "", "recovered": True,
            "prior_available_balance_usd": round(prior_available, 4),
            "current_available_balance_usd": round(current_available, 4),
        }
    remaining = max(0.0, cooldown_seconds - age)
    return {
        "blocked": True,
        "reason": (
            f"recent Bybit 110007 on {str(symbol).upper()}; free balance has not materially "
            f"recovered yet ({current_available:.2f} vs {prior_available:.2f} USDT); "
            f"retry after ~{remaining/60.0:.0f}m or earlier if capacity improves"
        ),
        "age_seconds": round(age, 1),
        "remaining_seconds": round(remaining, 1),
        "prior_available_balance_usd": round(prior_available, 4),
        "current_available_balance_usd": round(current_available, 4),
    }
