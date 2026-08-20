from __future__ import annotations

import time
from typing import Any

from bybit_client import BybitClient
from trading_store import set_state


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _position(client: BybitClient, symbol: str) -> dict[str, Any] | None:
    for row in client.get_positions(symbol=symbol):
        if _float(row.get("size"), 0.0) > 0:
            return row
    return None


def _executions(client: BybitClient, symbol: str, order_id: str, order_link_id: str) -> list[dict[str, Any]]:
    try:
        return client.get_executions(symbol=symbol, order_id=order_id, order_link_id=order_link_id, limit=50)
    except Exception:
        return []


def _fill_evidence(order: dict[str, Any], executions: list[dict[str, Any]]) -> dict[str, Any]:
    exec_qty = sum(max(0.0, _float(x.get("execQty"), 0.0)) for x in executions)
    order_qty = max(0.0, _float(order.get("cumExecQty"), 0.0))
    filled = exec_qty > 0 or order_qty > 0 or str(order.get("orderStatus", "")) == "Filled"
    avg_candidates = [_float(x.get("execPrice"), 0.0) for x in executions if _float(x.get("execPrice"), 0.0) > 0]
    avg_price = _float(order.get("avgPrice"), 0.0)
    if avg_price <= 0 and avg_candidates:
        avg_price = sum(avg_candidates) / len(avg_candidates)
    return {
        "filled": bool(filled),
        "exec_qty": exec_qty or order_qty,
        "avg_price": avg_price,
        "execution_count": len(executions),
    }


def _clear_lock() -> None:
    set_state("execution_safety_lock", "0")
    set_state("execution_safety_reason", "")


def confirm_market_entry(
    client: BybitClient,
    *,
    symbol: str,
    order_id: str = "",
    order_link_id: str = "",
    stop_loss: float,
    take_profit: float,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Confirm a Bybit entry using order + execution + position evidence.

    A create-order response is only an acknowledgement. Stan marks an entry executed only
    after observing actual fill evidence. A filled order may already be flat by the time the
    REST poll runs (for example, an attached TP/SL can close it quickly); that is still a real
    execution and must be distinguished from a false-positive acknowledgement.
    """
    deadline = time.time() + max(3.0, timeout_seconds)
    last_order: dict[str, Any] = {}
    last_position: dict[str, Any] | None = None
    last_execs: list[dict[str, Any]] = []
    while time.time() < deadline:
        try:
            rows = client.get_order_realtime(symbol=symbol, order_id=order_id, order_link_id=order_link_id)
            if not rows:
                rows = client.get_order_history(symbol=symbol, order_id=order_id, order_link_id=order_link_id, limit=5)
            if rows:
                last_order = rows[0]
        except Exception:
            pass
        try:
            last_execs = _executions(client, symbol, order_id, order_link_id)
        except Exception:
            last_execs = []
        try:
            last_position = _position(client, symbol)
        except Exception:
            last_position = None

        status = str(last_order.get("orderStatus", ""))
        fill = _fill_evidence(last_order, last_execs)
        if fill["filled"]:
            if last_position:
                # Reinforce protection only after an actual live position is observed. A protection
                # failure must never erase the fact that the fill happened; instead lock NEW entries
                # until the live position is verified protected or manually reconciled.
                protection_error = ""
                try:
                    client.set_trading_stop(symbol=symbol, stop_loss=str(stop_loss), take_profit=str(take_profit))
                except Exception as exc:
                    protection_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.25)
                position_after = _position(client, symbol) or last_position
                stop_seen = _float(position_after.get("stopLoss"), 0.0)
                tp_seen = _float(position_after.get("takeProfit"), 0.0)
                protected = stop_seen > 0 and tp_seen > 0
                if protected:
                    _clear_lock()
                    lifecycle = "filled_open_protected"
                else:
                    reason = (
                        f"Confirmed fill on {symbol}, but protective SL/TP could not be verified. "
                        "New entries are locked until protection is reconciled."
                    )
                    if protection_error:
                        reason += f" Protection error: {protection_error}"
                    set_state("execution_safety_lock", "1")
                    set_state("execution_safety_reason", reason)
                    set_state("execution_uncertain_symbol", symbol)
                    set_state("execution_uncertain_order_id", str(last_order.get("orderId") or order_id))
                    set_state("execution_uncertain_order_link_id", str(last_order.get("orderLinkId") or order_link_id))
                    set_state("execution_uncertain_stop", str(stop_loss))
                    set_state("execution_uncertain_tp", str(take_profit))
                    lifecycle = "filled_open_protection_unverified"
                return {
                    "confirmed": True,
                    "filled": True,
                    "position_open": True,
                    "protected": protected,
                    "lifecycle": lifecycle,
                    "order_status": status or "Filled",
                    "order_id": str(last_order.get("orderId") or order_id),
                    "order_link_id": str(last_order.get("orderLinkId") or order_link_id),
                    "avg_price": fill["avg_price"] or last_order.get("avgPrice"),
                    "cum_exec_qty": fill["exec_qty"],
                    "execution_count": fill["execution_count"],
                    "position_size": position_after.get("size"),
                    "position_side": position_after.get("side"),
                    "stop_loss": position_after.get("stopLoss"),
                    "take_profit": position_after.get("takeProfit"),
                    "protection_error": protection_error,
                }
            if status in {"Filled", "PartiallyFilledCanceled", "Cancelled"} or fill["exec_qty"] > 0:
                # Real fill evidence exists but the position is already flat. Do not lie that an
                # open position exists; preserve the fill lifecycle for later trade-history audit.
                _clear_lock()
                return {
                    "confirmed": True,
                    "filled": True,
                    "position_open": False,
                    "lifecycle": "filled_flat_before_confirmation",
                    "order_status": status,
                    "order_id": str(last_order.get("orderId") or order_id),
                    "order_link_id": str(last_order.get("orderLinkId") or order_link_id),
                    "avg_price": fill["avg_price"] or last_order.get("avgPrice"),
                    "cum_exec_qty": fill["exec_qty"],
                    "execution_count": fill["execution_count"],
                    "executions": last_execs[:10],
                }
        if status in {"Rejected", "Cancelled", "Deactivated"}:
            _clear_lock()
            return {
                "confirmed": False,
                "filled": False,
                "terminal": True,
                "lifecycle": "terminal_without_fill",
                "order_status": status,
                "order": last_order,
            }
        time.sleep(0.6)

    fill = _fill_evidence(last_order, last_execs)
    if fill["filled"]:
        # If the final poll has fill evidence, truthfully record it even without a live position.
        protected = False
        protection_error = ""
        lifecycle = "filled_flat_before_confirmation"
        if last_position:
            try:
                client.set_trading_stop(symbol=symbol, stop_loss=str(stop_loss), take_profit=str(take_profit))
            except Exception as exc:
                protection_error = f"{type(exc).__name__}: {exc}"
            try:
                last_position = _position(client, symbol) or last_position
            except Exception:
                pass
            protected = _float(last_position.get("stopLoss"), 0.0) > 0 and _float(last_position.get("takeProfit"), 0.0) > 0
            lifecycle = "filled_open_protected" if protected else "filled_open_protection_unverified"
        if not last_position or protected:
            _clear_lock()
        else:
            reason = f"Confirmed fill on {symbol}, but protective SL/TP could not be verified. New entries are locked until protection is reconciled."
            if protection_error:
                reason += f" Protection error: {protection_error}"
            set_state("execution_safety_lock", "1")
            set_state("execution_safety_reason", reason)
            set_state("execution_uncertain_symbol", symbol)
            set_state("execution_uncertain_order_id", str(last_order.get("orderId") or order_id))
            set_state("execution_uncertain_order_link_id", str(last_order.get("orderLinkId") or order_link_id))
            set_state("execution_uncertain_stop", str(stop_loss))
            set_state("execution_uncertain_tp", str(take_profit))
        return {
            "confirmed": True,
            "filled": True,
            "position_open": bool(last_position),
            "protected": protected if last_position else None,
            "lifecycle": lifecycle,
            "order_status": str(last_order.get("orderStatus", "")),
            "order_id": str(last_order.get("orderId") or order_id),
            "order_link_id": str(last_order.get("orderLinkId") or order_link_id),
            "avg_price": fill["avg_price"],
            "cum_exec_qty": fill["exec_qty"],
            "execution_count": fill["execution_count"],
            "position": last_position or {},
            "executions": last_execs[:10],
            "protection_error": protection_error,
        }

    reason = f"Could not reconcile Bybit order {order_link_id or order_id} within {timeout_seconds:.0f}s and no fill evidence was found. New entries are locked until reconciliation."
    set_state("execution_safety_lock", "1")
    set_state("execution_safety_reason", reason)
    set_state("execution_uncertain_symbol", symbol)
    set_state("execution_uncertain_order_id", order_id)
    set_state("execution_uncertain_order_link_id", order_link_id)
    set_state("execution_uncertain_stop", str(stop_loss))
    set_state("execution_uncertain_tp", str(take_profit))
    return {
        "confirmed": False,
        "filled": False,
        "terminal": False,
        "uncertain": True,
        "lifecycle": "ack_unreconciled_no_fill_evidence",
        "reason": reason,
        "order": last_order,
        "position": last_position or {},
        "executions": last_execs[:10],
    }


def reconcile_execution_lock(client: BybitClient) -> dict[str, Any]:
    from trading_store import get_state

    if get_state("execution_safety_lock", "0") != "1":
        return {"locked": False, "reconciled": True}
    symbol = get_state("execution_uncertain_symbol", "")
    order_id = get_state("execution_uncertain_order_id", "")
    order_link_id = get_state("execution_uncertain_order_link_id", "")
    try:
        stop_loss = float(get_state("execution_uncertain_stop", "0") or 0)
        take_profit = float(get_state("execution_uncertain_tp", "0") or 0)
    except Exception:
        stop_loss = take_profit = 0.0
    if not symbol:
        return {"locked": True, "reconciled": False, "reason": get_state("execution_safety_reason", "Unresolved execution state")}
    rows = client.get_order_realtime(symbol=symbol, order_id=order_id, order_link_id=order_link_id)
    if not rows:
        rows = client.get_order_history(symbol=symbol, order_id=order_id, order_link_id=order_link_id, limit=5)
    order = rows[0] if rows else {}
    executions = _executions(client, symbol, order_id, order_link_id)
    fill = _fill_evidence(order, executions)
    status = str(order.get("orderStatus", ""))
    pos = _position(client, symbol)
    if fill["filled"]:
        protection_error = ""
        protected = not bool(pos)
        if pos and stop_loss > 0 and take_profit > 0:
            try:
                client.set_trading_stop(symbol=symbol, stop_loss=str(stop_loss), take_profit=str(take_profit))
            except Exception as exc:
                protection_error = f"{type(exc).__name__}: {exc}"
            try:
                pos = _position(client, symbol) or pos
            except Exception:
                pass
            protected = _float(pos.get("stopLoss"), 0.0) > 0 and _float(pos.get("takeProfit"), 0.0) > 0
        if protected:
            _clear_lock()
        return {
            "locked": not protected,
            "reconciled": bool(protected),
            "filled": True,
            "position_open": bool(pos),
            "protected": protected if pos else None,
            "status": status,
            "position": pos or {},
            "executions": executions[:10],
            "protection_error": protection_error,
        }
    if status in {"Rejected", "Cancelled", "Deactivated"} and not pos:
        _clear_lock()
        return {"locked": False, "reconciled": True, "filled": False, "status": status}
    return {"locked": True, "reconciled": False, "filled": False, "status": status, "position": pos or {}, "order": order, "executions": executions[:10]}
