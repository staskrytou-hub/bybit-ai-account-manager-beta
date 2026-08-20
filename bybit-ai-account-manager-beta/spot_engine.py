from __future__ import annotations

import math
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from account_os_store import get_state, record_event, set_state
from bybit_client import BybitClient
from market_analysis import atr, parse_klines, completed_klines
from spot_ai import analyze_spot_candidate
from trading_config import load_trading_settings
from trading_usage import reserve_ai_call, release_ai_reservation, legacy_paced_daily_call_cap
from runtime_control import RuntimeStoppedError, manual_stop_active, runtime_stop_requested
from resilience import is_provider_availability_error
from trade_proposal import (
    build_spot_proposal, veto_blocks_proposal, record_proposal_veto, clear_proposal_veto,
    record_proposal_approval, reusable_proposal_approval, clear_proposal_approval,
    bump_proposal_stat,
)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _ret(closes: list[float], bars: int) -> float:
    if len(closes) <= bars or not closes[-1 - bars]:
        return 0.0
    return (closes[-1] / closes[-1 - bars] - 1.0) * 100.0


def _slope_pct(values: list[float]) -> float:
    if len(values) < 3 or not values[-1]:
        return 0.0
    n = len(values)
    sx = n * (n - 1) / 2.0
    sy = sum(values)
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sxy = sum(i * v for i, v in enumerate(values))
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / den if den else 0.0
    return slope / max(abs(values[-1]), 1e-12) * 100.0


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _quantize_step(value: float, step: str, *, up: bool = False) -> float:
    st = _dec(step or "0")
    if st <= 0:
        return float(value)
    q = (_dec(value) / st).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * st
    return float(q)


def _fmt(value: float, step: str = "0.00000001") -> str:
    st = _dec(step or "0.00000001")
    decimals = max(0, -st.as_tuple().exponent)
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".") or "0"


def _wallet_coin(wallet: dict[str, Any], coin: str) -> dict[str, Any]:
    target = coin.upper()
    for account in list(wallet.get("list") or []):
        for row in list(account.get("coin") or []):
            if str(row.get("coin", "")).upper() == target:
                return dict(row)
    return {}


def _equity(wallet: dict[str, Any]) -> float:
    items = list(wallet.get("list") or [])
    if not items:
        return 0.0
    row = items[0]
    return _f(row.get("totalEquity"), _f(row.get("totalWalletBalance")))


def build_spot_snapshot(client: BybitClient, symbol: str, interval: str = "15", instrument: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = client.get_kline(symbol, interval=interval, limit=240, category="spot")
    all_candles = parse_klines(rows)
    candles = completed_klines(all_candles, interval)
    if len(candles) < 80:
        raise RuntimeError(f"Not enough completed Spot history for {symbol}: {len(candles)} candles")
    ticker = client.get_ticker(symbol, category="spot")
    ob = client.get_orderbook(symbol, category="spot", limit=25)
    inst = instrument or client.get_instrument(symbol, category="spot")

    closes = [float(c["close"]) for c in candles]
    vols = [float(c["volume"]) for c in candles]
    signal_last = closes[-1]
    live_price = _f(ticker.get("lastPrice"), signal_last)
    a = atr(candles, 14)
    atr_pct = a / max(signal_last, 1e-12) * 100.0
    bar_rets = [((closes[i] / closes[i - 1]) - 1.0) * 100.0 if closes[i - 1] else 0.0 for i in range(1, len(closes))]
    slope20 = _slope_pct(closes[-20:])
    slope50 = _slope_pct(closes[-50:])
    vol_base = vols[-20:-1] if len(vols) >= 20 else vols[:-1]
    vm = _mean(vol_base) or 1.0
    vs = _std(vol_base)
    vz = (vols[-1] - vm) / vs if vs > 1e-12 else 0.0
    vr = vols[-1] / vm if vm else 1.0

    prior20 = candles[-21:-1]
    prior50 = candles[-51:-1]
    hi20 = max((float(x["high"]) for x in prior20), default=signal_last)
    lo20 = min((float(x["low"]) for x in prior20), default=signal_last)
    hi50 = max((float(x["high"]) for x in prior50), default=signal_last)
    lo50 = min((float(x["low"]) for x in prior50), default=signal_last)
    rp20 = (signal_last - lo20) / max(hi20 - lo20, 1e-12)
    rp50 = (signal_last - lo50) / max(hi50 - lo50, 1e-12)
    brk = (signal_last - hi20) / max(a, 1e-12)
    brd = (lo20 - signal_last) / max(a, 1e-12)
    vwap_num = sum(c * v for c, v in zip(closes[-20:], vols[-20:]))
    vwap_den = sum(vols[-20:]) or 1.0
    vwap = vwap_num / vwap_den
    vwap_dist = (signal_last / vwap - 1.0) * 100.0 if vwap else 0.0
    cur = candles[-1]
    body = abs(float(cur["close"]) - float(cur["open"])) / max(float(cur["high"]) - float(cur["low"]), 1e-12)

    bids = ob.get("b") or []
    asks = ob.get("a") or []
    bid = _f(bids[0][0]) if bids else _f(ticker.get("bid1Price"))
    ask = _f(asks[0][0]) if asks else _f(ticker.get("ask1Price"))
    mid = (bid + ask) / 2.0 if bid and ask else live_price
    spread = (ask - bid) / max(mid, 1e-12) * 10000.0 if bid and ask else 999.0
    bd = sum(_f(x[1]) for x in bids[:10])
    ad = sum(_f(x[1]) for x in asks[:10])
    imb = (bd - ad) / max(bd + ad, 1e-12)

    trend_evidence = min(1.0, max(0.0, slope20) * 22.0 + max(0.0, slope50) * 14.0)
    structure_evidence = min(1.0, max(0.0, brk) * 0.9 + max(0.0, rp20 - 0.5) * 1.2 + max(0.0, rp50 - 0.5) * 0.7)
    participation = min(1.0, max(0.0, vz) / 3.0 + max(0.0, vr - 1.0) * 0.35)
    micro = min(1.0, max(0.0, imb) * 2.0)
    liquidity = 1.0 if spread <= 8 else max(0.0, 1.0 - (spread - 8.0) / 50.0)
    setup = max(0.0, min(1.0, 0.28 * trend_evidence + 0.28 * structure_evidence + 0.22 * participation + 0.12 * micro + 0.10 * liquidity))

    lot = dict(inst.get("lotSizeFilter") or {})
    return {
        "symbol": symbol.upper(), "category": "spot", "interval": interval, "captured_at_ms": int(time.time() * 1000),
        "closed_candle_start_ms": int(candles[-1].get("start", 0) or 0),
        "forming_candle_start_ms": int(all_candles[-1].get("start", 0) or 0) if len(all_candles) > len(candles) else 0,
        "forming_candle_excluded": len(all_candles) > len(candles),
        "price": live_price, "signal_price": signal_last, "bid": bid, "ask": ask, "spread_bps": round(spread, 4), "atr14": round(a, 8), "atr_pct": round(atr_pct, 4),
        "return_1_pct": round(_ret(closes, 1), 4), "return_4_pct": round(_ret(closes, 4), 4),
        "return_12_pct": round(_ret(closes, 12), 4), "return_48_pct": round(_ret(closes, 48), 4),
        "trend_slope_20_pct": round(slope20, 6), "trend_slope_50_pct": round(slope50, 6), "realized_vol_20_pct": round(_std(bar_rets[-20:]), 5),
        "volume_ratio_20": round(vr, 4), "volume_z_20": round(vz, 4), "range_position_20": round(rp20, 4), "range_position_50": round(rp50, 4),
        "breakout_20_atr": round(brk, 4), "breakdown_20_atr": round(brd, 4), "vwap_distance_20_pct": round(vwap_dist, 4),
        "drawdown_from_20_high_pct": round((signal_last / hi20 - 1.0) * 100.0 if hi20 else 0.0, 4),
        "rebound_from_20_low_pct": round((signal_last / lo20 - 1.0) * 100.0 if lo20 else 0.0, 4), "body_strength": round(body, 4),
        "orderbook_imbalance_10": round(imb, 5), "ticker_24h_pct": _f(ticker.get("price24hPcnt")) * 100.0,
        "turnover_24h": _f(ticker.get("turnover24h")), "setup_strength": round(setup, 4),
        "local_bias": "buy_candidate" if setup >= 0.55 and slope20 > 0 else "watch",
        "base_coin": inst.get("baseCoin", symbol.upper().removesuffix("USDT")), "quote_coin": inst.get("quoteCoin", "USDT"),
        "symbol_type": inst.get("symbolType", ""), "st_tag": str(inst.get("stTag", "0")),
        "base_precision": str(lot.get("basePrecision", "0.00000001")), "quote_precision": str(lot.get("quotePrecision", "0.00000001")),
        "min_order_amt": _f(lot.get("minOrderAmt"), 5.0), "max_limit_order_qty": _f(lot.get("maxLimitOrderQty"), 0.0),
        "tick_size": str((inst.get("priceFilter") or {}).get("tickSize", "0.00000001")),
        "evidence_candle_policy": "completed_candles_only_v460",
        "evidence_model": "v4.6_spot_structural_closed_candle_integrity",
    }


def scan_spot_universe(*, testnet: bool = False, top_n: int = 12, interval: str = "15") -> list[dict[str, Any]]:
    client = BybitClient(testnet=testnet, authenticated=False)
    tickers = client.get_tickers("spot")
    instruments = {str(x.get("symbol", "")).upper(): x for x in client.get_instruments("spot") if isinstance(x, dict)}
    prelim: list[tuple[float, str]] = []
    for row in tickers:
        sym = str(row.get("symbol", "")).upper()
        inst = instruments.get(sym) or {}
        if not sym.endswith("USDT") or str(inst.get("status", "Trading")) != "Trading":
            continue
        turnover = max(0.0, _f(row.get("turnover24h")))
        bid, ask = _f(row.get("bid1Price")), _f(row.get("ask1Price"))
        mid = (bid + ask) / 2.0 if bid and ask else _f(row.get("lastPrice"))
        spread = (ask - bid) / max(mid, 1e-12) * 10000.0 if bid and ask else 999.0
        if turnover < 250000 or spread > 45:
            continue
        liq = min(1.0, math.log10(max(turnover, 1.0)) / 9.0)
        move = min(1.0, abs(_f(row.get("price24hPcnt"))) * 12.0)
        score = 0.75 * liq + 0.15 * move + 0.10 * max(0.0, 1.0 - spread / 45.0)
        prelim.append((score, sym))
    prelim.sort(reverse=True)
    out: list[dict[str, Any]] = []
    for _, sym in prelim[: max(top_n * 2, 16)]:
        try:
            snap = build_spot_snapshot(client, sym, interval, instruments.get(sym))
            if float(snap.get("spread_bps", 999)) > 35:
                continue
            out.append(snap)
        except Exception:
            continue
        if len(out) >= top_n:
            break
    out.sort(key=lambda x: (float(x.get("setup_strength", 0.0)), math.log10(max(float(x.get("turnover_24h", 0.0)), 1.0))), reverse=True)
    return out


def _current_stage() -> str:
    # Futures/overall learning state is persisted in the last AI assessment when available.
    try:
        from trading_store import get_state as trading_get_state
        import json
        raw = trading_get_state("last_assessment", "")
        data = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
        live = data.get("live_learning_state") if isinstance(data, dict) else {}
        stage = str((live or {}).get("growth_stage", "learning"))
        if stage in {"learning", "validated", "mature"}:
            return stage
    except Exception:
        pass
    return "learning"


def _allocation_pct(stage: str, cfg: dict[str, Any]) -> float:
    if stage == "mature":
        return float(cfg.get("spot_mature_max_allocation_pct", 45.0))
    if stage == "validated":
        return float(cfg.get("spot_validated_max_allocation_pct", 30.0))
    return float(cfg.get("spot_learning_max_allocation_pct", 18.0))


def _risk_pct(stage: str, cfg: dict[str, Any]) -> float:
    if stage == "mature":
        return float(cfg.get("spot_mature_risk_pct", 0.90))
    if stage == "validated":
        return float(cfg.get("spot_validated_risk_pct", 0.60))
    return float(cfg.get("spot_learning_risk_pct", 0.36))


def _active_spot_trade() -> dict[str, Any]:
    state = get_state("spot_active_trade", {})
    return state if isinstance(state, dict) else {}


def monitor_spot_trade(client: BybitClient) -> dict[str, Any]:
    trade = _active_spot_trade()
    if not trade or str(trade.get("state")) in {"closed", "cancelled", "error"}:
        return trade
    symbol = str(trade.get("symbol", ""))
    order_id = str(trade.get("order_id", ""))
    base = str(trade.get("base_coin", ""))
    if not symbol or not order_id:
        return trade
    try:
        rows = client.get_order_realtime(symbol=symbol, order_id=order_id, category="spot")
        if not rows:
            rows = client.get_order_history(symbol=symbol, order_id=order_id, category="spot")
        row = rows[0] if rows else {}
        status = str(row.get("orderStatus", ""))
        trade["entry_status"] = status
        trade["cum_exec_qty"] = _f(row.get("cumExecQty"), _f(trade.get("cum_exec_qty")))
        trade["avg_price"] = _f(row.get("avgPrice"), _f(trade.get("avg_price")))
        if status in {"Cancelled", "Rejected", "Deactivated"}:
            trade["state"] = "cancelled"
        elif status in {"Filled", "PartiallyFilled"} and trade["cum_exec_qty"] > 0:
            trade["state"] = "live"
            if not bool(trade.get("execution_counted")):
                trade["execution_counted"] = True
                bump_proposal_stat("confirmed", lane="spot", symbol=symbol, extra={"order_id": order_id, "cum_exec_qty": trade.get("cum_exec_qty"), "avg_price": trade.get("avg_price")})
                bump_proposal_stat("executed", lane="spot", symbol=symbol, extra={"order_id": order_id, "cum_exec_qty": trade.get("cum_exec_qty"), "avg_price": trade.get("avg_price")})
        # If entry stays unfilled too long, cancel it instead of leaving stale capital exposure.
        age = time.time() - float(trade.get("created_ts", time.time()))
        if status in {"New", "PartiallyFilled"} and age > float(load_trading_settings().get("spot_entry_timeout_seconds", 90)):
            client.cancel_order(symbol=symbol, order_id=order_id, category="spot")
            trade["state"] = "cancel_requested"

        if trade.get("state") == "live" and base:
            wallet = client.get_unified_wallet(base)
            current = _f(_wallet_coin(wallet, base).get("walletBalance"))
            pre = _f(trade.get("pre_base_balance"))
            filled = max(_f(trade.get("cum_exec_qty")), _f(trade.get("qty")))
            trade["current_base_balance"] = current
            # Attached TP/SL should remove the Stan-acquired increment. This is a conservative reconciliation heuristic.
            if filled > 0 and current <= pre + filled * 0.08:
                trade["state"] = "closed"
                trade["closed_ts"] = time.time()
                record_event("spot.closed", f"Spot trade reconciled closed: {symbol}", trade)
        set_state("spot_active_trade", trade)
        return trade
    except Exception as exc:
        trade["monitor_error"] = f"{type(exc).__name__}: {exc}"
        set_state("spot_active_trade", trade)
        return trade


def assess_and_maybe_execute_spot(
    snapshot: dict[str, Any],
    *,
    capabilities: dict[str, Any],
    research_context: list[dict[str, Any]] | None = None,
    event_context: list[dict[str, Any]] | None = None,
    allow_live: bool = True,
) -> dict[str, Any]:
    if runtime_stop_requested():
        return {"action": "blocked", "reason": "manual_stop"}
    cfg = load_trading_settings()
    setup = float(snapshot.get("setup_strength", 0.0) or 0.0)
    approved = [x for x in (research_context or []) if bool(x.get("robust")) and str(x.get("symbol", "")).upper() == str(snapshot.get("symbol", "")).upper()]
    proposal = build_spot_proposal(snapshot, cfg, strategy_supported=bool(approved))
    set_state("last_spot_proposal", proposal)
    if not bool(proposal.get("eligible")):
        return {"action": "watch", "reason": str(proposal.get("reason") or "no executable Spot proposal"), "snapshot": snapshot, "proposal": proposal}
    veto_blocked, veto_reason = veto_blocks_proposal(proposal, str(snapshot.get("interval", "15")), "spot")
    if veto_blocked:
        return {"action": "watch", "reason": veto_reason, "snapshot": snapshot, "proposal": proposal}

    # Deterministic execution funnel: do not buy model tokens for a candidate that the
    # Spot execution layer would certainly reject.
    spread = float(snapshot.get("spread_bps", 999.0) or 999.0)
    max_spread = float(cfg.get("spot_max_spread_bps", 22.0))
    if spread > max_spread:
        return {"action": "watch", "reason": f"deterministic pre-AI block: spread {spread:.2f} bps > {max_spread:.2f}", "snapshot": snapshot, "proposal": proposal}
    equity_hint = float(capabilities.get("equity_usdt", 0.0) or 0.0)
    min_order_amt = float(snapshot.get("min_order_amt", 0.0) or 0.0)
    if equity_hint > 0 and min_order_amt > equity_hint * float(cfg.get("spot_min_order_max_allocation_pct", 35.0)) / 100.0:
        return {"action": "watch", "reason": "deterministic pre-AI block: exchange minimum order exceeds safe Spot allocation envelope", "snapshot": snapshot, "proposal": proposal}

    strong = str(proposal.get("priority", "normal")) == "high"
    closed_candle = int(snapshot.get("closed_candle_start_ms", 0) or 0)
    signature = str(proposal.get("signature", ""))
    interval = str(snapshot.get("interval", "15"))
    symbol = str(snapshot.get("symbol", ""))
    analysis_key = f"spot:{symbol}:{interval}:{closed_candle}" if closed_candle > 0 else ""
    if analysis_key and analysis_key == str(get_state("spot_last_completed_analysis_key", "") or ""):
        return {"action": "watch", "reason": "exact spot snapshot/candle already completed", "snapshot": snapshot}
    if analysis_key and analysis_key == str(get_state("spot_last_failed_analysis_key", "") or ""):
        failed_at = float(get_state("spot_last_failed_analysis_at", 0.0) or 0.0)
        if failed_at and time.time() - failed_at < 300:
            return {"action": "watch", "reason": "recent Spot AI failure cooldown; deterministic monitoring continues", "snapshot": snapshot}

    cached_approval = reusable_proposal_approval(proposal, interval, "spot")
    use_cached_approval = bool(cached_approval)
    proposal_cooldown_key = f"spot-proposal:{symbol}:{interval}"
    spot_pacing_enabled = bool(cfg.get("spot_entry_pacing_enabled", True))
    spot_pacing_hours = int(cfg.get("spot_entry_pacing_window_hours", 4) or 4)
    spot_normal_pacing = legacy_paced_daily_call_cap(int(cfg.get("spot_entry_verify_calls_daily", 7)), lane="normal", window_hours=spot_pacing_hours) if spot_pacing_enabled else {}
    spot_reserve_pacing = legacy_paced_daily_call_cap(int(cfg.get("spot_entry_reserve_calls_daily", 4)), lane="reserve", window_hours=spot_pacing_hours) if spot_pacing_enabled else {}

    proposal_kind = "spot_entry_cached_approval" if use_cached_approval else "spot_entry_verify"
    if use_cached_approval:
        bump_proposal_stat("created", lane="spot", symbol=symbol, extra={"quality": proposal.get("quality"), "priority": proposal.get("priority")})
        bump_proposal_stat("ai_reused", lane="spot", symbol=symbol, extra={"confidence": cached_approval.get("confidence"), "model": cached_approval.get("model")})
    else:
        # v4.6.4: consume normal Spot verification capacity first. Reserve capacity is used
        # only when the normal pool is exhausted and the proposal is high priority.
        allowed_ai, governor_reason = reserve_ai_call(
            "spot_entry_verify",
            budget=int(cfg.get("trading_token_budget_daily", 0)),
            estimated_tokens=2200 if strong else 1900,
            max_calls=int(cfg.get("ai_max_calls_daily", 0)),
            kind_budget=int(cfg.get("spot_entry_verify_tokens_daily", 18000)),
            kind_max_calls=int(cfg.get("spot_entry_verify_calls_daily", 7)),
            kind_paced_max_calls=int(spot_normal_pacing.get("paced_max_calls", 0) or 0),
            kind_pacing_next_unlock=str(spot_normal_pacing.get("next_unlock_at", "") or ""),
            cooldown_key=proposal_cooldown_key,
            cooldown_seconds=int(cfg.get("spot_proposal_reverify_minutes", 45)) * 60,
            signature=signature,
        )
        if (not allowed_ai and strong and any(x in str(governor_reason).lower() for x in ("cap reached", "pacing cap reached", "token budget reached", "token reserve would exceed budget"))):
            proposal_kind = "spot_entry_reserve"
            allowed_ai, governor_reason = reserve_ai_call(
                "spot_entry_reserve",
                budget=int(cfg.get("trading_token_budget_daily", 0)),
                estimated_tokens=2200,
                max_calls=int(cfg.get("ai_max_calls_daily", 0)),
                kind_budget=int(cfg.get("spot_entry_reserve_tokens_daily", 10000)),
                kind_max_calls=int(cfg.get("spot_entry_reserve_calls_daily", 4)),
                kind_paced_max_calls=int(spot_reserve_pacing.get("paced_max_calls", 0) or 0),
                kind_pacing_next_unlock=str(spot_reserve_pacing.get("next_unlock_at", "") or ""),
                cooldown_key=f"spot-reserve:{symbol}:{interval}",
                cooldown_seconds=int(cfg.get("spot_proposal_reverify_minutes", 45)) * 60,
                signature=signature,
            )
        if not allowed_ai:
            return {"action": "watch", "reason": f"AI governor: {governor_reason}", "snapshot": snapshot, "proposal": proposal}
        bump_proposal_stat("created", lane="spot", symbol=symbol, extra={"quality": proposal.get("quality"), "priority": proposal.get("priority"), "ai_lane": proposal_kind})

    include_news = False  # news verification is deferred until an actual BUY candidate exists
    if runtime_stop_requested():
        return {"action": "blocked", "reason": "manual_stop", "snapshot": snapshot}
    try:
        if use_cached_approval:
            assessment = {
                "action": "buy",
                "confidence": float(cached_approval.get("confidence", 0.74) or 0.74),
                "thesis": "Reused recent AI APPROVE because the Spot proposal signature is unchanged.",
                "entry": float(proposal.get("entry", 0.0) or 0.0),
                "stop_loss": float(proposal.get("stop_loss", 0.0) or 0.0),
                "take_profit": float(proposal.get("take_profit", 0.0) or 0.0),
                "risk_notes": ["Cached AI approval reused; live wallet/risk/order checks remain authoritative."],
                "used_news": False, "proposal_verdict": "approved_cached",
            }
            usage = {"total_tokens": 0}
            model = str(cached_approval.get("model") or "cached-approval")
        else:
            assessment, usage, model = analyze_spot_candidate(snapshot, research_context=research_context, event_context=event_context, include_news=False, trade_proposal=proposal, usage_kind=proposal_kind)
    except RuntimeStoppedError:
        return {"action": "blocked", "reason": "manual_stop", "snapshot": snapshot}
    except Exception as exc:
        if is_provider_availability_error(exc):
            release_ai_reservation(proposal_cooldown_key)
            record_event("spot.ai_provider_paused", f"Spot AI provider paused: {snapshot.get('symbol')}", {"analysis_key": analysis_key, "error": f"{type(exc).__name__}: {exc}"})
            return {"action": "watch", "reason": "AI PAUSED — provider unavailable; deterministic Spot monitoring continues", "snapshot": snapshot, "ai_paused": True}
        if analysis_key:
            set_state("spot_last_failed_analysis_key", analysis_key)
            set_state("spot_last_failed_analysis_at", time.time())
        record_event("spot.ai.error", f"Spot AI failed safely: {snapshot.get('symbol')}", {"analysis_key": analysis_key, "error": f"{type(exc).__name__}: {exc}"})
        return {"action": "watch", "reason": f"Spot AI failed safely: {type(exc).__name__}: {exc}", "snapshot": snapshot, "ai_failed": True}

    if not use_cached_approval:
        bump_proposal_stat("ai_verified", lane="spot", symbol=symbol)
    if str(assessment.get("action", "hold")).lower() == "buy":
        assessment["entry"] = float(proposal.get("entry", snapshot.get("price", 0.0)) or 0.0)
        assessment["stop_loss"] = float(proposal.get("stop_loss", 0.0) or 0.0)
        assessment["take_profit"] = float(proposal.get("take_profit", 0.0) or 0.0)
        assessment["trade_proposal"] = proposal
        assessment["proposal_verdict"] = "approved_cached" if use_cached_approval else "approved"
        clear_proposal_veto(symbol, interval, "spot")
        if not use_cached_approval:
            record_proposal_approval(
                symbol, interval, signature=signature, action="buy",
                confidence=float(assessment.get("confidence", 0.0) or 0.0), model=str(model),
                minutes=int(cfg.get("spot_proposal_approval_minutes", 45)), lane="spot",
            )
            bump_proposal_stat("ai_approved", lane="spot", symbol=symbol, extra={"quality": proposal.get("quality")})
    else:
        assessment = dict(assessment)
        assessment["action"] = "hold"
        assessment["entry"] = float(snapshot.get("price", 0.0) or 0.0)
        assessment["stop_loss"] = 0.0
        assessment["take_profit"] = 0.0
        assessment["trade_proposal"] = proposal
        assessment["proposal_verdict"] = "vetoed"
        veto_text = str(assessment.get("thesis") or assessment.get("invalidation") or "AI vetoed Spot proposal")
        clear_proposal_approval(symbol, interval, "spot")
        record_proposal_veto(str(snapshot.get("symbol", "")), str(snapshot.get("interval", "15")), signature=str(proposal.get("signature", "")), reason=veto_text, action="buy", minutes=int(cfg.get("spot_proposal_veto_minutes", 90)), lane="spot")
        bump_proposal_stat("ai_vetoed", lane="spot", symbol=str(snapshot.get("symbol", "")), extra={"quality": proposal.get("quality"), "reason": veto_text[:240]})

    # Only a preliminary BUY can justify a second web/news-enabled model call. HOLDs end here.
    if str(assessment.get("action", "hold")).lower() == "buy" and bool(cfg.get("news_enabled", True)) and setup >= float(cfg.get("spot_news_threshold", 0.80)) and bool(event_context):
        try:
            news_allowed, news_reason = reserve_ai_call(
                "spot_news",
                kind_budget=int(cfg.get("spot_news_tokens_daily", 10000)),
                kind_max_calls=int(cfg.get("spot_news_calls_daily", 2)),
                cooldown_key=f"spot-news:{snapshot.get('symbol')}:{snapshot.get('interval','15')}",
                cooldown_seconds=int(cfg.get("news_cooldown_minutes", 180)) * 60,
                signature=analysis_key,
            )
            if not news_allowed:
                return {"action": "watch", "reason": f"Spot BUY withheld: news verification governor: {news_reason}", "snapshot": snapshot}
            verified, news_usage, news_model = analyze_spot_candidate(snapshot, research_context=research_context, event_context=event_context, include_news=True, trade_proposal=proposal, usage_kind="spot_news")
            verified["pre_news_action"] = "buy"
            if str(verified.get("action", "hold")).lower() == "buy":
                verified["entry"] = float(proposal.get("entry",0.0) or 0.0)
                verified["stop_loss"] = float(proposal.get("stop_loss",0.0) or 0.0)
                verified["take_profit"] = float(proposal.get("take_profit",0.0) or 0.0)
                verified["trade_proposal"] = proposal
                verified["proposal_verdict"] = "approved_after_news"
            else:
                verified["action"] = "hold"
                verified["entry"] = float(snapshot.get("price",0.0) or 0.0)
                verified["stop_loss"] = 0.0
                verified["take_profit"] = 0.0
                verified["trade_proposal"] = proposal
                verified["proposal_verdict"] = "vetoed_after_news"
                record_proposal_veto(str(snapshot.get("symbol", "")), str(snapshot.get("interval", "15")), signature=str(proposal.get("signature", "")), reason=str(verified.get("thesis") or "news veto"), action="buy", minutes=max(120, int(cfg.get("spot_proposal_veto_minutes",90))), lane="spot")
            assessment, usage, model = verified, news_usage, news_model
            include_news = True
        except RuntimeStoppedError:
            return {"action": "blocked", "reason": "manual_stop", "snapshot": snapshot}
        except Exception as news_exc:
            if is_provider_availability_error(news_exc):
                release_ai_reservation(proposal_cooldown_key)
            record_event("spot.news_verification.error", f"Spot BUY verification unavailable: {snapshot.get('symbol')}", {"analysis_key": analysis_key, "error": f"{type(news_exc).__name__}: {news_exc}"})
            return {"action": "watch", "reason": "Spot BUY withheld because news/provider verification was unavailable", "snapshot": snapshot, "ai_paused": is_provider_availability_error(news_exc)}
    if runtime_stop_requested():
        return {"action": "blocked", "reason": "manual_stop", "snapshot": snapshot}
    if analysis_key:
        set_state("spot_last_completed_analysis_key", analysis_key)
        set_state("spot_last_failed_analysis_key", "")
        set_state("spot_last_failed_analysis_at", 0.0)
    result: dict[str, Any] = {"action": assessment.get("action"), "assessment": assessment, "usage": usage, "model": model, "snapshot": snapshot, "analysis_key": analysis_key, "proposal": proposal}
    if str(assessment.get("action")) != "buy":
        result["execution"] = "hold"
        return result

    confidence = float(assessment.get("confidence", 0.0) or 0.0)
    min_conf = float(cfg.get("spot_min_confidence", 0.72))
    if str(snapshot.get("st_tag", "0")) == "1":
        min_conf += 0.08
    if not approved:
        min_conf += float(cfg.get("spot_no_oos_confidence_bump", 0.03))
    if confidence < min_conf:
        result["execution"] = "blocked"
        result["reason"] = f"confidence {confidence:.2f} < required {min_conf:.2f}"
        return result
    if float(snapshot.get("spread_bps", 999)) > float(cfg.get("spot_max_spread_bps", 22.0)):
        result["execution"] = "blocked"; result["reason"] = "spread_too_wide"; return result
    entry = float(assessment.get("entry", 0.0) or snapshot.get("price", 0.0) or 0.0)
    stop = float(assessment.get("stop_loss", 0.0) or 0.0)
    target = float(assessment.get("take_profit", 0.0) or 0.0)
    if not (0 < stop < entry < target):
        result["execution"] = "blocked"; result["reason"] = "invalid_buy_geometry"; return result
    rr = (target - entry) / max(entry - stop, 1e-12)
    if rr < float(cfg.get("spot_min_reward_risk", 1.35)):
        result["execution"] = "blocked"; result["reason"] = f"reward_risk {rr:.2f} too low"; return result

    if not allow_live or not bool(capabilities.get("spot_trade")):
        result["execution"] = "permission_required"
        result["reason"] = "Enable Bybit API permission SPOT -> Trading (SpotTrade) to arm live Spot execution."
        return result
    if _active_spot_trade() and str(_active_spot_trade().get("state")) not in {"closed", "cancelled", "error"}:
        result["execution"] = "blocked"; result["reason"] = "one_Stan_spot_trade_already_active"; return result

    if runtime_stop_requested():
        result["execution"] = "blocked"; result["reason"] = "manual_stop"; return result
    client = BybitClient(testnet=bool(capabilities.get("testnet")), authenticated=True)
    wallet = client.get_unified_wallet()
    equity = _equity(wallet)
    usdt = _f(_wallet_coin(wallet, "USDT").get("walletBalance"), equity)
    if equity <= 0 or usdt <= 0:
        result["execution"] = "blocked"; result["reason"] = "no_available_usdt"; return result
    stage = _current_stage()
    allocation_cap = equity * _allocation_pct(stage, cfg) / 100.0
    risk_cash = equity * _risk_pct(stage, cfg) / 100.0
    stop_pct = (entry - stop) / max(entry, 1e-12)
    desired_notional = min(risk_cash / max(stop_pct, 1e-6), allocation_cap, usdt * 0.97)
    min_amt = max(0.0, float(snapshot.get("min_order_amt", 0.0) or 0.0))
    overridden = False
    if desired_notional < min_amt:
        min_risk = min_amt * stop_pct
        hard_risk_cash = equity * float(cfg.get("spot_absolute_risk_cap_pct", 1.50)) / 100.0
        hard_allocation = equity * float(cfg.get("spot_min_order_max_allocation_pct", 35.0)) / 100.0
        if min_risk <= hard_risk_cash and min_amt <= min(hard_allocation, usdt * 0.97):
            desired_notional = min_amt * 1.002
            overridden = True
        else:
            result["execution"] = "blocked"
            result["reason"] = "exchange_minimum_spot_order_exceeds_safe_account_envelope"
            result["min_order_risk_usdt"] = min_risk
            return result

    ask = float(snapshot.get("ask", 0.0) or entry)
    tick = str(snapshot.get("tick_size", "0.00000001"))
    base_step = str(snapshot.get("base_precision", "0.00000001"))
    entry_cross_bps = float(cfg.get("spot_entry_cross_bps", 4.0))
    limit_price = _quantize_step(max(ask, entry) * (1.0 + entry_cross_bps / 10000.0), tick, up=True)
    qty = _quantize_step(desired_notional / max(limit_price, 1e-12), base_step, up=False)
    if qty * limit_price < min_amt:
        qty = _quantize_step(min_amt * 1.002 / max(limit_price, 1e-12), base_step, up=True)
    if qty <= 0:
        result["execution"] = "blocked"; result["reason"] = "spot_qty_zero_after_rounding"; return result
    stop_q = _quantize_step(stop, tick, up=False)
    target_q = _quantize_step(target, tick, up=True)
    actual_risk = qty * max(limit_price - stop_q, 0.0)
    if actual_risk > equity * float(cfg.get("spot_absolute_risk_cap_pct", 1.50)) / 100.0 + 1e-9:
        result["execution"] = "blocked"; result["reason"] = "rounded_spot_order_exceeds_absolute_risk_cap"; return result

    bump_proposal_stat("risk_passed", lane="spot", symbol=str(snapshot.get("symbol", "")), extra={"actual_risk": actual_risk, "rr": rr})
    base = str(snapshot.get("base_coin", ""))
    pre_wallet = client.get_unified_wallet(base) if base else {}
    pre_base = _f(_wallet_coin(pre_wallet, base).get("walletBalance")) if base else 0.0
    link = f"stan-s-{int(time.time())}-{str(snapshot.get('symbol',''))[:8]}"[:36]
    if runtime_stop_requested():
        result["execution"] = "blocked"; result["reason"] = "manual_stop"; return result
    ack = client.place_spot_order(
        symbol=str(snapshot.get("symbol")), side="Buy", qty=_fmt(qty, base_step), order_type="Limit",
        price=_fmt(limit_price, tick), time_in_force="GTC", order_link_id=link,
        take_profit=_fmt(target_q, tick), stop_loss=_fmt(stop_q, tick), tp_order_type="Market", sl_order_type="Market",
    )
    order = dict(ack.get("result") or {})
    trade = {
        "state": "submitted", "symbol": str(snapshot.get("symbol")), "base_coin": base, "qty": qty,
        "notional_usdt": qty * limit_price, "entry_limit": limit_price, "stop_loss": stop_q, "take_profit": target_q,
        "actual_risk_usdt": actual_risk, "actual_risk_pct": actual_risk / max(equity, 1e-12) * 100.0,
        "min_order_override": overridden, "growth_stage": stage, "confidence": confidence,
        "order_id": str(order.get("orderId", "")), "order_link_id": link, "created_ts": time.time(), "pre_base_balance": pre_base,
    }
    set_state("spot_active_trade", trade)
    record_event("spot.entry.submitted", f"Spot entry submitted: {trade['symbol']}", trade)
    bump_proposal_stat("submitted", lane="spot", symbol=str(snapshot.get("symbol", "")), extra={"notional": trade.get("notional_usdt"), "risk": trade.get("actual_risk_usdt"), "order_id": trade.get("order_id")})
    result["execution"] = "submitted"
    result["trade"] = trade
    return result
