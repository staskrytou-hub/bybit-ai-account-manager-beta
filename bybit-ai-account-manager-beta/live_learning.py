from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bybit_client import BybitClient
from trading_config import load_trading_settings
from trading_store import get_state, set_state


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _pnl(row: dict[str, Any]) -> float:
    try:
        return float(row.get("closedPnl") or 0.0)
    except Exception:
        return 0.0


def _time(row: dict[str, Any]) -> int:
    for key in ("updatedTime", "createdTime"):
        try:
            return int(float(row.get(key) or 0))
        except Exception:
            pass
    return 0


def _performance_metrics(rows: list[dict[str, Any]], equity: float) -> dict[str, Any]:
    ordered = sorted(rows, key=_time)
    pnls = [_pnl(x) for x in ordered]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-12 else (9.99 if gross_profit > 0 else 0.0)
    expectancy = (sum(pnls) / len(pnls)) if pnls else 0.0

    curve = 0.0
    peak = 0.0
    max_dd_cash = 0.0
    for value in pnls:
        curve += value
        peak = max(peak, curve)
        max_dd_cash = max(max_dd_cash, peak - curve)
    max_dd_pct = (max_dd_cash / max(float(equity), 1e-9)) * 100.0

    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round((len(wins) / len(pnls)) if pnls else 0.0, 4),
        "gross_profit": round(gross_profit, 8),
        "gross_loss": round(gross_loss, 8),
        "profit_factor": round(min(profit_factor, 9.99), 4),
        "expectancy_cash": round(expectancy, 8),
        "max_drawdown_cash": round(max_dd_cash, 8),
        "max_drawdown_pct_of_current_equity": round(max_dd_pct, 4),
    }


def _growth_stage(metrics: dict[str, Any], weekly_pnl: float, loss_streak: int, cfg: dict[str, Any]) -> str:
    trades = int(metrics.get("trades", 0) or 0)
    pf = float(metrics.get("profit_factor", 0.0) or 0.0)
    expectancy = float(metrics.get("expectancy_cash", 0.0) or 0.0)
    drawdown = float(metrics.get("max_drawdown_pct_of_current_equity", 999.0) or 999.0)

    mature_ok = (
        trades >= int(cfg.get("growth_mature_min_trades", 100))
        and pf >= float(cfg.get("growth_mature_min_profit_factor", 1.20))
        and expectancy > 0
        and drawdown <= float(cfg.get("growth_mature_max_drawdown_pct", 6.0))
        and weekly_pnl > 0
        and loss_streak < 2
    )
    if mature_ok:
        return "mature"

    validated_ok = (
        trades >= int(cfg.get("growth_validated_min_trades", 40))
        and pf >= float(cfg.get("growth_validated_min_profit_factor", 1.10))
        and expectancy > 0
        and drawdown <= float(cfg.get("growth_validated_max_drawdown_pct", 6.0))
        and weekly_pnl >= 0
    )
    if validated_ok:
        return "validated"
    return "learning"


def live_learning_snapshot(client: BybitClient, equity: float) -> dict[str, Any]:
    """Build deterministic adaptive growth/risk state from real Bybit closed PnL.

    Stan may earn a larger *allowed envelope* after enough positive evidence, but can never
    exceed the absolute risk/leverage ceilings in trading settings. Losses demote risk quickly.
    Positive PnL has no profit cap.
    """
    cfg = load_trading_settings()
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = now - timedelta(days=7)

    daily_rows = client.get_closed_pnl(limit=200, start_time_ms=_ms(day_start), end_time_ms=_ms(now))
    weekly_rows = client.get_closed_pnl(limit=200, start_time_ms=_ms(week_start), end_time_ms=_ms(now))
    learning_started_raw = get_state("autopilot_learning_started_ms", "")
    try:
        learning_started_ms = int(learning_started_raw) if learning_started_raw else None
    except Exception:
        learning_started_ms = None
    recent_rows = client.get_closed_pnl(limit=200, start_time_ms=learning_started_ms, end_time_ms=_ms(now)) if learning_started_ms else client.get_closed_pnl(limit=200)

    daily_pnl = sum(_pnl(x) for x in daily_rows)
    weekly_pnl = sum(_pnl(x) for x in weekly_rows)
    recent_sorted = sorted(recent_rows, key=_time, reverse=True)

    loss_streak = 0
    for row in recent_sorted:
        value = _pnl(row)
        if value < 0:
            loss_streak += 1
        elif value > 0:
            break

    metrics = _performance_metrics(recent_rows, equity)
    stage = _growth_stage(metrics, weekly_pnl, loss_streak, cfg)
    trades = int(metrics.get("trades", 0) or 0)

    if stage == "mature":
        target_risk_pct = float(cfg.get("growth_mature_risk_pct", 6.00))
        leverage_cap = float(cfg.get("growth_mature_leverage_cap", 3.0))
        exposure_cap_pct = float(cfg.get("growth_mature_exposure_pct", 250.0))
        max_trades_today = int(cfg.get("growth_mature_max_trades_day", 18))
        max_positions_allowed = int(cfg.get("growth_mature_max_positions", 5))
        portfolio_risk_cap_pct = float(cfg.get("portfolio_mature_risk_cap_pct", 25.0))
    elif stage == "validated":
        target_risk_pct = float(cfg.get("growth_validated_risk_pct", 5.00))
        leverage_cap = float(cfg.get("growth_validated_leverage_cap", 2.5))
        exposure_cap_pct = float(cfg.get("growth_validated_exposure_pct", 175.0))
        max_trades_today = int(cfg.get("growth_validated_max_trades_day", 14))
        max_positions_allowed = int(cfg.get("growth_validated_max_positions", 4))
        portfolio_risk_cap_pct = float(cfg.get("portfolio_validated_risk_cap_pct", 25.0))
    else:
        # The first few trades are calibration; after that the account can use the normal learning target.
        target_risk_pct = float(cfg.get("growth_calibration_risk_pct", 3.00)) if trades < int(cfg.get("growth_calibration_trades", 6)) else float(cfg.get("growth_learning_risk_pct", 4.00))
        leverage_cap = float(cfg.get("growth_learning_leverage_cap", 2.0))
        exposure_cap_pct = float(cfg.get("growth_learning_exposure_pct", 125.0))
        max_trades_today = int(cfg.get("growth_learning_max_trades_day", 10))
        max_positions_allowed = int(cfg.get("growth_learning_max_positions", 3))
        portfolio_risk_cap_pct = float(cfg.get("portfolio_learning_risk_cap_pct", 25.0))

    absolute_risk_cap = float(cfg.get("absolute_risk_cap_pct", 7.00))
    portfolio_risk_cap_pct = min(portfolio_risk_cap_pct, float(cfg.get("portfolio_absolute_risk_cap_pct", 25.0)))
    portfolio_cash_cap = max(0.0, float(cfg.get("portfolio_absolute_risk_cap_usdt", 20.0) or 0.0))
    if portfolio_cash_cap > 0 and float(equity) > 0:
        portfolio_risk_cap_pct = min(portfolio_risk_cap_pct, portfolio_cash_cap / float(equity) * 100.0)
    target_risk_pct = min(target_risk_pct, absolute_risk_cap)
    leverage_cap = min(leverage_cap, float(cfg.get("max_leverage", 3.0)))

    multiplier = 1.0
    confidence_bump = 0.0
    notes: list[str] = [
        f"growth stage={stage}; recent trades={trades}; PF={float(metrics.get('profit_factor',0)):.2f}; drawdown={float(metrics.get('max_drawdown_pct_of_current_equity',0)):.2f}%"
    ]
    if trades < int(cfg.get("growth_calibration_trades", 6)):
        notes.append("calibration phase: deliberately smaller risk while Stan learns live execution quality")
    if loss_streak >= 2:
        multiplier *= 0.55
        confidence_bump += 0.04
        notes.append(f"recent loss streak={loss_streak}; risk reduced and selectivity increased")
    if weekly_pnl < 0:
        multiplier *= 0.70
        confidence_bump += 0.02
        notes.append("7-day realized PnL is negative; temporary conservative bias applied")
    if float(metrics.get("profit_factor", 0.0) or 0.0) < 0.90 and trades >= 12:
        multiplier *= 0.70
        confidence_bump += 0.02
        notes.append("recent profit factor below 0.90; growth governor reduced risk")

    pause = False
    # v4.6.9: a loss streak no longer turns the bot off for hours.  Existing adaptive
    # risk/selectivity reductions above remain authoritative, but the London/New York
    # sessions are not discarded merely because the previous trades lost.  True hard
    # stops (daily/weekly realized loss, execution safety, unprotected positions) remain.
    time_pause_enabled = bool(cfg.get("loss_streak_time_pause_enabled", False))
    if time_pause_enabled:
        pause_hours = int(cfg.get("loss_streak_pause_hours", 8))
        threshold = int(cfg.get("loss_streak_pause_after", 3))
        latest_trade_marker = str(_time(recent_sorted[0])) if recent_sorted else "0"
        previous_trigger = get_state("live_loss_pause_trigger", "")
        pause_until_raw = get_state("live_loss_pause_until", "")
        pause_until = None
        try:
            pause_until = datetime.fromisoformat(pause_until_raw) if pause_until_raw else None
        except Exception:
            pause_until = None
        if loss_streak >= threshold and latest_trade_marker and latest_trade_marker != previous_trigger:
            pause_until = now + timedelta(hours=pause_hours)
            set_state("live_loss_pause_until", pause_until.isoformat())
            set_state("live_loss_pause_trigger", latest_trade_marker)
        if pause_until is not None and now < pause_until:
            pause = True
            multiplier = 0.0
            notes.append(f"loss-streak safety pause active until {pause_until.isoformat(timespec='minutes')}")
    else:
        # Migrate away from v4.6.8 persisted pause state so a restart does not inherit an
        # obsolete daytime shutdown.  Risk percentages themselves are not changed here.
        if get_state("live_loss_pause_until", "") or get_state("live_loss_pause_trigger", ""):
            set_state("live_loss_pause_until", "")
            set_state("live_loss_pause_trigger", "")
        if loss_streak >= int(cfg.get("loss_streak_pause_after", 3)):
            notes.append("loss streak handled by adaptive risk/selectivity; time-based trading shutdown disabled")

    day_limit = max(0.0, float(equity)) * float(cfg.get("max_daily_loss_pct", 25.0)) / 100.0
    week_limit = max(0.0, float(equity)) * float(cfg.get("max_weekly_loss_pct", 30.0)) / 100.0
    # v4.6.2 user-selected absolute account loss-at-stop envelope. The cash cap dominates
    # the percentage cap on larger balances and prevents this aggressive profile from
    # silently scaling beyond the requested dollar amount.
    if portfolio_cash_cap > 0:
        day_limit = min(day_limit, portfolio_cash_cap) if day_limit > 0 else portfolio_cash_cap
        week_limit = min(week_limit, portfolio_cash_cap) if week_limit > 0 else portfolio_cash_cap
    if day_limit > 0 and daily_pnl <= -day_limit:
        pause = True
        multiplier = 0.0
        notes.append("daily realized loss hard-stop reached")
    if week_limit > 0 and weekly_pnl <= -week_limit:
        pause = True
        multiplier = 0.0
        notes.append("7-day realized loss hard-stop reached")

    effective_risk_pct = target_risk_pct * max(0.0, min(1.0, multiplier))
    return {
        "daily_pnl": round(daily_pnl, 8),
        "weekly_pnl": round(weekly_pnl, 8),
        "trades_today": len(daily_rows),
        "recent_closed_trades": trades,
        "learning_started_ms": learning_started_ms,
        "loss_streak": loss_streak,
        "growth_stage": stage,
        "performance_metrics": metrics,
        "target_risk_pct": round(target_risk_pct, 4),
        "effective_risk_pct": round(effective_risk_pct, 4),
        "risk_multiplier": round(max(0.0, min(1.0, multiplier)), 4),
        "leverage_cap": round(leverage_cap, 4),
        "exposure_cap_pct": round(exposure_cap_pct, 4),
        "max_trades_today_allowed": max_trades_today,
        "max_positions_allowed": max_positions_allowed,
        "portfolio_risk_cap_pct": round(portfolio_risk_cap_pct, 4),
        "portfolio_risk_cap_usdt": round(min(portfolio_cash_cap, float(equity) * portfolio_risk_cap_pct / 100.0) if portfolio_cash_cap > 0 else float(equity) * portfolio_risk_cap_pct / 100.0, 4),
        "confidence_bump": round(confidence_bump, 4),
        "pause": pause,
        "notes": notes,
    }
