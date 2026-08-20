from __future__ import annotations

import math
import time
from typing import Any

from trading_config import load_trading_settings
from trading_store import paper_daily_pnl, get_paper_position
from research_store import get_research_state


def _floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step + 1e-12) * step


def _ceil_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step - 1e-12) * step


def evaluate_trade_candidate(
    assessment: dict[str, Any],
    snapshot: dict[str, Any],
    instrument: dict[str, Any] | None = None,
    *,
    equity: float | None = None,
    open_positions: int = 0,
    daily_realized_pnl: float | None = None,
    weekly_realized_pnl: float | None = None,
    trades_today: int = 0,
    learning_risk_multiplier: float = 1.0,
    adaptive_risk_pct: float | None = None,
    leverage_cap: float | None = None,
    exposure_cap_pct: float | None = None,
    max_trades_today_allowed: int | None = None,
    max_positions_allowed: int | None = None,
    growth_stage: str = "",
    performance_metrics: dict[str, Any] | None = None,
    confidence_bump: float = 0.0,
    safety_pause: bool = False,
    learning_notes: list[str] | None = None,
    portfolio_open_risk_pct: float = 0.0,
    portfolio_risk_cap_pct: float | None = None,
    same_symbol_open: bool = False,
    unprotected_positions: list[str] | None = None,
    max_directional_correlation: dict[str, Any] | None = None,
    available_balance_usd: float | None = None,
) -> dict[str, Any]:
    cfg = load_trading_settings()
    action = str(assessment.get("action", "hold")).lower()
    confidence = float(assessment.get("confidence", 0.0) or 0.0)
    entry = float(assessment.get("entry", snapshot.get("price", 0.0)) or snapshot.get("price", 0.0))
    stop = float(assessment.get("stop_loss", 0.0) or 0.0)
    take_profit = float(assessment.get("take_profit", 0.0) or 0.0)
    price = float(snapshot.get("price", 0.0) or 0.0)
    mode = str(cfg.get("mode"))
    reasons: list[str] = []

    # v4.6.0 state-coherence hard guard.  A decision is only valid for the exact
    # symbol/interval/closed candle it analyzed.  This is intentionally tolerant of
    # legacy assessments that predate the identity fields, but a known mismatch is
    # always a deterministic execution block.
    ass_symbol = str(assessment.get("analysis_symbol") or "").upper()
    snap_symbol = str(snapshot.get("symbol") or "").upper()
    ass_interval = str(assessment.get("analysis_interval") or "")
    snap_interval = str(snapshot.get("interval") or "")
    ass_candle = int(assessment.get("analysis_closed_candle_start_ms", 0) or 0)
    snap_candle = int(snapshot.get("closed_candle_start_ms", 0) or 0)
    if ass_symbol and snap_symbol and ass_symbol != snap_symbol:
        reasons.append(f"state coherence mismatch: assessment {ass_symbol} cannot size snapshot {snap_symbol}")
    if ass_interval and snap_interval and ass_interval != snap_interval:
        reasons.append(f"state coherence mismatch: assessment interval {ass_interval} != snapshot interval {snap_interval}")
    if ass_candle and snap_candle and ass_candle != snap_candle:
        reasons.append("state coherence mismatch: assessment and snapshot refer to different closed candles")

    # A HOLD is a completed AI decision, not a failed sizing attempt.  Older builds
    # continued through quantity/minimum-order checks and produced misleading
    # "below instrument minimum" noise even though no entry was requested.
    if action not in {"long", "short"}:
        equity_value = float(equity if equity and equity > 0 else cfg["paper_start_equity"])
        stage_exposure_pct = float(exposure_cap_pct if exposure_cap_pct is not None else cfg.get("max_notional_pct_equity", 125.0))
        stage_leverage_cap = float(leverage_cap if leverage_cap is not None else cfg.get("max_leverage", 3.0))
        live_position_limit = int(max_positions_allowed if max_positions_allowed is not None else cfg.get("max_positions", 5))
        live_trade_limit = int(max_trades_today_allowed if max_trades_today_allowed is not None else cfg.get("max_trades_per_day", 16))
        portfolio_cap = float(portfolio_risk_cap_pct if portfolio_risk_cap_pct is not None else cfg.get("portfolio_learning_risk_cap_pct", 25.0))
        portfolio_cap = min(portfolio_cap, float(cfg.get("portfolio_absolute_risk_cap_pct", 25.0)))
        portfolio_cash_cap = max(0.0, float(cfg.get("portfolio_absolute_risk_cap_usdt", 20.0) or 0.0))
        if portfolio_cash_cap > 0 and equity_value > 0:
            portfolio_cap = min(portfolio_cap, portfolio_cash_cap / equity_value * 100.0)
        base_risk_pct = float(adaptive_risk_pct if adaptive_risk_pct is not None else cfg.get("risk_per_trade_pct", 4.00))
        base_risk_pct = min(base_risk_pct, float(cfg.get("absolute_risk_cap_pct", 7.00)))
        multiplier = max(0.0, min(1.0, float(learning_risk_multiplier)))
        return {
            "allowed": False,
            "reasons": reasons + ["assessment action is HOLD; no entry sizing was attempted"],
            "action": action,
            "confidence": confidence,
            "required_confidence": round(float(cfg.get("min_confidence", 0.62)) + max(0.0, float(confidence_bump)), 4),
            "entry": entry,
            "stop_loss": stop,
            "take_profit": take_profit,
            "qty": 0.0,
            "notional_usdt": 0.0,
            "notional_cap_usdt": round(equity_value * max(0.05, stage_exposure_pct / 100.0), 4) if mode == "autopilot_live" else round(float(cfg.get("max_notional_usdt", 50.0)), 4),
            "exposure_cap_pct_equity": round(stage_exposure_pct, 2),
            "risk_cash": 0.0,
            "target_risk_cash": round(equity_value * base_risk_pct * multiplier / 100.0, 4),
            "actual_risk_per_trade_pct": 0.0,
            "minimum_executable_qty": 0.0,
            "min_order_override_used": False,
            "min_order_override_reason": "",
            "absolute_risk_cap_pct": float(cfg.get("absolute_risk_cap_pct", 7.00)),
            "adaptive_base_risk_pct": round(base_risk_pct, 4),
            "effective_risk_per_trade_pct": round(base_risk_pct * multiplier, 4),
            "learning_risk_multiplier": round(multiplier, 4),
            "growth_stage": growth_stage or "static",
            "performance_metrics": dict(performance_metrics or {}),
            "learning_notes": list(learning_notes or []),
            "reward_risk": 0.0,
            "leverage": 0.0,
            "leverage_cap": round(stage_leverage_cap, 4),
            "equity_basis": round(equity_value, 4),
            "daily_realized_pnl": daily_realized_pnl,
            "weekly_realized_pnl": weekly_realized_pnl,
            "trades_today": int(trades_today),
            "max_trades_today_allowed": live_trade_limit,
            "max_positions_allowed": live_position_limit,
            "portfolio_open_risk_pct": round(float(portfolio_open_risk_pct or 0.0), 4),
            "projected_portfolio_risk_pct": round(float(portfolio_open_risk_pct or 0.0), 4),
            "portfolio_risk_cap_pct": round(portfolio_cap, 4),
            "portfolio_risk_cap_usdt": round(equity_value * portfolio_cap / 100.0, 4),
            "same_symbol_open": bool(same_symbol_open),
            "unprotected_positions": list(unprotected_positions or []),
            "max_directional_correlation": dict(max_directional_correlation or {}),
            "account_available_balance_usd": round(max(0.0, float(available_balance_usd or 0.0)), 4) if available_balance_usd is not None else None,
            "margin_utilization_pct": float(cfg.get("futures_available_balance_utilization_pct", 82.0)),
            "margin_reserve_usdt": float(cfg.get("futures_available_balance_reserve_usdt", 2.0)),
            "margin_budget_usd": 0.0,
            "margin_notional_cap_usdt": 0.0,
            "required_initial_margin_estimate_usd": 0.0,
            "margin_resized": False,
            "margin_resize_reason": "",
            "sizing_evaluated": False,
        }

    if mode in {"testnet", "autopilot_live"} and get_research_state("bootstrap_complete", "0") != "1":
        reasons.append("professional first-run research bootstrap has not completed yet")
    if safety_pause:
        reasons.append("learning/risk governor has paused new entries")
    if action not in {"long", "short"}:
        reasons.append("assessment action is HOLD")
    required_confidence = min(0.95, float(cfg["min_confidence"]) + max(0.0, float(confidence_bump)))
    if confidence < required_confidence:
        reasons.append(f"confidence {confidence:.2f} is below current minimum {required_confidence:.2f}")
    if float(snapshot.get("spread_bps", 0.0) or 0.0) > float(cfg["max_spread_bps"]):
        reasons.append(f"spread {snapshot.get('spread_bps')} bps exceeds limit")
    captured_ms = float(snapshot.get("captured_at_ms", 0.0) or 0.0)
    if captured_ms > 0:
        age_seconds = max(0.0, time.time() - captured_ms / 1000.0)
        if age_seconds > float(cfg.get("max_data_age_seconds", 90)):
            reasons.append(f"market data is stale ({age_seconds:.1f}s old)")
    live_position_limit = int(max_positions_allowed if max_positions_allowed is not None else cfg["max_positions"])
    if open_positions >= live_position_limit:
        reasons.append(f"current growth-stage maximum open positions reached ({live_position_limit})")
    if same_symbol_open and mode in {"autopilot_live", "testnet"}:
        reasons.append("a live position is already open on this symbol; Portfolio Learning rotates to another market instead of pyramiding")
    if unprotected_positions and bool(cfg.get("portfolio_block_unprotected_positions", True)) and mode in {"autopilot_live", "testnet"}:
        reasons.append("existing live position(s) are missing a verified stop-loss: " + ", ".join(unprotected_positions[:4]))
    corr_info = dict(max_directional_correlation or {})
    if bool(cfg.get("portfolio_learning_enabled", True)) and mode in {"autopilot_live", "testnet"} and corr_info.get("too_correlated"):
        reasons.append(
            f"portfolio overlap too high with {corr_info.get('symbol')}: directional correlation "
            f"{float(corr_info.get('directional_overlap',0) or 0):.2f}"
        )
    if get_paper_position(str(snapshot.get("symbol", ""))) and mode == "paper":
        reasons.append("paper position already open for this symbol")
    live_trade_limit = int(max_trades_today_allowed if max_trades_today_allowed is not None else cfg.get("max_trades_per_day", 16))
    if int(trades_today) >= live_trade_limit and mode == "autopilot_live":
        reasons.append(f"current growth-stage trade limit reached ({live_trade_limit}/UTC day)")
    if price <= 0 or entry <= 0:
        reasons.append("invalid market/entry price")
    if stop <= 0 or take_profit <= 0:
        reasons.append("hard stop-loss and take-profit are required")
    if action == "long" and not (stop < entry < take_profit):
        reasons.append("LONG requires stop < entry < take-profit")
    if action == "short" and not (take_profit < entry < stop):
        reasons.append("SHORT requires take-profit < entry < stop")

    equity_value = float(equity if equity and equity > 0 else cfg["paper_start_equity"])
    margin_utilization_pct = float(cfg.get("futures_available_balance_utilization_pct", 82.0) or 82.0)
    margin_utilization = max(0.10, min(0.95, margin_utilization_pct / 100.0))
    margin_reserve_usdt = max(0.0, float(cfg.get("futures_available_balance_reserve_usdt", 2.0) or 0.0))
    account_available_balance = max(0.0, float(available_balance_usd or 0.0)) if available_balance_usd is not None else None
    margin_budget_usd = 0.0
    if account_available_balance is not None:
        margin_budget_usd = max(0.0, account_available_balance - margin_reserve_usdt) * margin_utilization
    margin_resized = False
    margin_resize_reason = ""
    margin_notional_cap = 0.0

    if mode == "paper":
        daily_pnl = paper_daily_pnl()
        max_loss = equity_value * float(cfg["max_daily_loss_pct"]) / 100.0
        cash_loss_cap = max(0.0, float(cfg.get("portfolio_absolute_risk_cap_usdt", 20.0) or 0.0))
        if cash_loss_cap > 0:
            max_loss = min(max_loss, cash_loss_cap)
        if daily_pnl <= -max_loss:
            reasons.append(f"daily paper loss limit reached ({daily_pnl:.2f} <= {-max_loss:.2f})")
    elif mode == "autopilot_live":
        if daily_realized_pnl is not None:
            max_loss = equity_value * float(cfg["max_daily_loss_pct"]) / 100.0
            cash_loss_cap = max(0.0, float(cfg.get("portfolio_absolute_risk_cap_usdt", 20.0) or 0.0))
            if cash_loss_cap > 0:
                max_loss = min(max_loss, cash_loss_cap)
            if float(daily_realized_pnl) <= -max_loss:
                reasons.append("daily live loss limit reached")
        if weekly_realized_pnl is not None:
            max_week_loss = equity_value * float(cfg.get("max_weekly_loss_pct", 30.0)) / 100.0
            cash_loss_cap = max(0.0, float(cfg.get("portfolio_absolute_risk_cap_usdt", 20.0) or 0.0))
            if cash_loss_cap > 0:
                max_week_loss = min(max_week_loss, cash_loss_cap)
            if float(weekly_realized_pnl) <= -max_week_loss:
                reasons.append("7-day live loss limit reached")

    stop_distance = abs(entry - stop) if entry and stop else 0.0
    hard_absolute_risk_pct = float(cfg.get("absolute_risk_cap_pct", 7.00))
    base_risk_pct = float(adaptive_risk_pct if adaptive_risk_pct is not None else cfg.get("risk_per_trade_pct", 4.00))
    base_risk_pct = min(base_risk_pct, hard_absolute_risk_pct)
    multiplier = max(0.0, min(1.0, float(learning_risk_multiplier)))
    effective_risk_pct = base_risk_pct * multiplier
    risk_cash = equity_value * effective_risk_pct / 100.0
    qty = risk_cash / stop_distance if stop_distance > 0 else 0.0

    # Live exposure scales with equity and evidence; there is intentionally no fixed-dollar profit ceiling.
    stage_exposure_pct = float(exposure_cap_pct if exposure_cap_pct is not None else cfg.get("max_notional_pct_equity", 125.0))
    stage_leverage_cap = float(leverage_cap if leverage_cap is not None else cfg.get("max_leverage", 3.0))
    stage_leverage_cap = min(stage_leverage_cap, float(cfg.get("max_leverage", 3.0)))
    if mode == "autopilot_live":
        equity_exposure_cap = equity_value * max(0.05, stage_exposure_pct / 100.0)
        margin_based_cap = equity_value * max(1.0, stage_leverage_cap) * 0.90
        notional_cap = min(equity_exposure_cap, margin_based_cap)
    else:
        fixed_cap = float(cfg.get("max_notional_usdt", 50.0))
        notional_cap = fixed_cap
    max_qty_by_notional = notional_cap / entry if entry > 0 else 0.0
    qty = min(qty, max_qty_by_notional) if qty > 0 else 0.0

    qty_step = 0.0
    min_qty = 0.0
    leverage_step = 0.01
    instrument_max_leverage = stage_leverage_cap
    if instrument:
        lot = instrument.get("lotSizeFilter") or {}
        levf = instrument.get("leverageFilter") or {}
        try: qty_step = float(lot.get("qtyStep") or 0)
        except Exception: pass
        try: min_qty = float(lot.get("minOrderQty") or 0)
        except Exception: pass
        try: instrument_max_leverage = min(stage_leverage_cap, float(levf.get("maxLeverage") or stage_leverage_cap))
        except Exception: pass
        try: leverage_step = float(levf.get("leverageStep") or leverage_step)
        except Exception: pass
    min_notional_value = 0.0
    if instrument:
        lot = instrument.get("lotSizeFilter") or {}
        try: min_notional_value = float(lot.get("minNotionalValue") or 0)
        except Exception: pass

    # v4.6.6: account-capacity sizing. Bybit UTA totalAvailableBalance already reflects
    # initial margin consumed by an existing BTC/Futures position. Use only a bounded
    # fraction of the remaining capacity, then raise leverage only as much as required
    # inside the stage/instrument cap. If the target still does not fit, shrink qty locally
    # before any order is sent instead of discovering 110007 after paid AI.
    if mode == "autopilot_live" and account_available_balance is not None:
        if margin_budget_usd <= 0:
            reasons.append("no usable Bybit available balance remains after the live-position margin reserve")
            qty = 0.0
            notional_cap = 0.0
        else:
            margin_notional_cap = margin_budget_usd * max(1.0, instrument_max_leverage)
            if margin_notional_cap < notional_cap:
                notional_cap = margin_notional_cap
            current_notional = max(0.0, qty * entry)
            if current_notional > margin_notional_cap + 1e-9:
                prior_qty = qty
                qty = margin_notional_cap / entry if entry > 0 else 0.0
                margin_resized = True
                margin_resize_reason = (
                    f"available-balance resize: qty {prior_qty:.12g} -> {qty:.12g}; "
                    f"free-margin notional cap {margin_notional_cap:.4f} USDT"
                )

    qty = _floor_step(qty, qty_step) if qty_step else qty
    target_qty = qty
    target_risk_cash = risk_cash
    min_order_override_used = False
    min_order_override_reason = ""

    minimum_exec_qty = max(min_qty, (min_notional_value / entry) if min_notional_value > 0 and entry > 0 else 0.0)
    if minimum_exec_qty > 0 and qty_step > 0:
        minimum_exec_qty = _ceil_step(minimum_exec_qty, qty_step)

    if qty <= 0 or (minimum_exec_qty and qty + 1e-12 < minimum_exec_qty):
        if bool(cfg.get("executable_min_order_override", True)) and minimum_exec_qty > 0 and stop_distance > 0 and mode in {"autopilot_live", "testnet", "paper"}:
            candidate_qty = minimum_exec_qty
            candidate_notional = candidate_qty * entry
            candidate_risk_cash = candidate_qty * stop_distance
            candidate_risk_pct = candidate_risk_cash / max(equity_value, 1e-9) * 100.0
            override_risk_cap = min(hard_absolute_risk_pct, float(cfg.get("min_order_override_max_risk_pct", 7.00)))
            override_multiple = max(1.0, float(cfg.get("min_order_override_max_target_multiple", 4.0)))
            if mode == "autopilot_live":
                override_exposure_cap = equity_value * max(0.05, stage_exposure_pct / 100.0) * override_multiple
                margin_based_cap = equity_value * max(1.0, stage_leverage_cap) * 0.90
                override_notional_cap = min(override_exposure_cap, margin_based_cap)
                if account_available_balance is not None:
                    override_notional_cap = min(override_notional_cap, margin_budget_usd * max(1.0, instrument_max_leverage))
            else:
                override_notional_cap = float(cfg.get("max_notional_usdt", 50.0)) * override_multiple

            if candidate_risk_pct <= override_risk_cap + 1e-12 and candidate_notional <= override_notional_cap + 1e-9:
                qty = candidate_qty
                risk_cash = candidate_risk_cash
                min_order_override_used = True
                min_order_override_reason = (
                    f"minimum executable order used: target qty {target_qty:.12g} -> {candidate_qty:.12g}; "
                    f"actual risk {candidate_risk_pct:.3f}% <= override cap {override_risk_cap:.3f}%"
                )
            else:
                reasons.append(
                    "instrument minimum is not safely executable: "
                    f"minimum qty {candidate_qty:.12g} would risk {candidate_risk_pct:.3f}% "
                    f"and use {candidate_notional:.4f} USDT notional"
                )
        else:
            reasons.append("calculated quantity is below the instrument minimum inside the current risk envelope")

    notional = qty * entry
    actual_risk_cash = qty * stop_distance if qty > 0 and stop_distance > 0 else 0.0
    actual_risk_pct = actual_risk_cash / max(equity_value, 1e-9) * 100.0
    portfolio_cap = float(portfolio_risk_cap_pct if portfolio_risk_cap_pct is not None else cfg.get("portfolio_learning_risk_cap_pct", 25.0))
    portfolio_cap = min(portfolio_cap, float(cfg.get("portfolio_absolute_risk_cap_pct", 25.0)))
    portfolio_cash_cap = max(0.0, float(cfg.get("portfolio_absolute_risk_cap_usdt", 20.0) or 0.0))
    if portfolio_cash_cap > 0 and equity_value > 0:
        portfolio_cap = min(portfolio_cap, portfolio_cash_cap / equity_value * 100.0)
    projected_portfolio_risk_pct = max(0.0, float(portfolio_open_risk_pct or 0.0)) + max(0.0, actual_risk_pct)
    if bool(cfg.get("portfolio_learning_enabled", True)) and mode in {"autopilot_live", "testnet"} and projected_portfolio_risk_pct > portfolio_cap + 1e-12:
        reasons.append(
            f"portfolio risk would reach {projected_portfolio_risk_pct:.3f}% above current stage cap {portfolio_cap:.3f}%"
        )
    # Use the smallest practical leverage for the chosen exposure. With an existing live
    # position, free collateral can be much smaller than account equity, so the minimum
    # leverage must also be high enough for this order to fit inside current UTA capacity.
    required_leverage_equity = max(1.0, (notional / max(equity_value, 1e-9)) * 1.10)
    required_leverage_margin = 1.0
    if mode == "autopilot_live" and account_available_balance is not None and notional > 0:
        if margin_budget_usd > 0:
            required_leverage_margin = max(1.0, notional / margin_budget_usd)
        else:
            required_leverage_margin = float("inf")
    required_leverage = max(required_leverage_equity, required_leverage_margin)
    selected_leverage = _ceil_step(required_leverage, leverage_step) if math.isfinite(required_leverage) else instrument_max_leverage
    selected_leverage = max(1.0, min(selected_leverage, instrument_max_leverage))
    if required_leverage > instrument_max_leverage + 1e-9:
        reasons.append("position exposure would require leverage above the current growth-stage/available-balance cap")
    required_initial_margin_estimate = notional / max(selected_leverage, 1e-9) if notional > 0 else 0.0
    if mode == "autopilot_live" and account_available_balance is not None and required_initial_margin_estimate > margin_budget_usd + 1e-9:
        reasons.append(
            f"estimated initial margin {required_initial_margin_estimate:.4f} exceeds current usable Bybit available balance {margin_budget_usd:.4f}"
        )

    reward = abs(take_profit - entry) if take_profit and entry else 0.0
    rr = reward / stop_distance if stop_distance > 0 else 0.0
    min_rr = float(cfg.get("min_reward_risk", 1.30))
    if rr < min_rr and action in {"long", "short"}:
        reasons.append(f"reward/risk {rr:.2f} is below {min_rr:.2f}")

    allowed = len(reasons) == 0
    return {
        "allowed": allowed,
        "reasons": reasons,
        "action": action,
        "confidence": confidence,
        "required_confidence": round(required_confidence, 4),
        "entry": entry,
        "stop_loss": stop,
        "take_profit": take_profit,
        "qty": round(qty, 12),
        "notional_usdt": round(notional, 4),
        "notional_cap_usdt": round(notional_cap, 4),
        "exposure_cap_pct_equity": round(stage_exposure_pct, 2),
        "risk_cash": round(actual_risk_cash, 4),
        "target_risk_cash": round(target_risk_cash, 4),
        "actual_risk_per_trade_pct": round(actual_risk_pct, 4),
        "minimum_executable_qty": round(minimum_exec_qty, 12),
        "min_order_override_used": min_order_override_used,
        "min_order_override_reason": min_order_override_reason,
        "absolute_risk_cap_pct": hard_absolute_risk_pct,
        "adaptive_base_risk_pct": round(base_risk_pct, 4),
        "effective_risk_per_trade_pct": round(effective_risk_pct, 4),
        "learning_risk_multiplier": round(multiplier, 4),
        "growth_stage": growth_stage or "static",
        "performance_metrics": dict(performance_metrics or {}),
        "learning_notes": list(learning_notes or []),
        "reward_risk": round(rr, 3),
        "leverage": round(float(selected_leverage), 4),
        "leverage_cap": round(float(instrument_max_leverage), 4),
        "equity_basis": round(equity_value, 4),
        "daily_realized_pnl": daily_realized_pnl,
        "weekly_realized_pnl": weekly_realized_pnl,
        "trades_today": int(trades_today),
        "max_trades_today_allowed": live_trade_limit,
        "max_positions_allowed": live_position_limit,
        "portfolio_open_risk_pct": round(float(portfolio_open_risk_pct or 0.0), 4),
        "projected_portfolio_risk_pct": round(projected_portfolio_risk_pct, 4),
        "portfolio_risk_cap_pct": round(portfolio_cap, 4),
        "portfolio_risk_cap_usdt": round(equity_value * portfolio_cap / 100.0, 4),
        "same_symbol_open": bool(same_symbol_open),
        "unprotected_positions": list(unprotected_positions or []),
        "max_directional_correlation": dict(max_directional_correlation or {}),
        "account_available_balance_usd": round(account_available_balance, 4) if account_available_balance is not None else None,
        "margin_utilization_pct": round(margin_utilization_pct, 2),
        "margin_reserve_usdt": round(margin_reserve_usdt, 4),
        "margin_budget_usd": round(margin_budget_usd, 4),
        "margin_notional_cap_usdt": round(margin_notional_cap, 4),
        "required_initial_margin_estimate_usd": round(required_initial_margin_estimate, 4),
        "margin_resized": bool(margin_resized),
        "margin_resize_reason": margin_resize_reason,
        "required_leverage_from_available_balance": round(required_leverage_margin, 4) if math.isfinite(required_leverage_margin) else None,
        "sizing_evaluated": True,
    }
