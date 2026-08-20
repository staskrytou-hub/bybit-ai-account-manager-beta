from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v466-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


class CapacityClient:
    def get_unified_wallet(self):
        return {
            "list": [{
                "accountType": "UNIFIED",
                "totalEquity": "79.00",
                "totalWalletBalance": "78.70",
                "totalMarginBalance": "79.10",
                "totalAvailableBalance": "11.00",
                "totalInitialMargin": "12.40",
                "totalMaintenanceMargin": "0.75",
                "totalPerpUPL": "0.40",
                "accountIMRate": "0.15",
                "accountMMRate": "0.01",
                "coin": [{
                    "coin": "USDT", "walletBalance": "78.70", "equity": "79.00",
                    "locked": "0", "totalOrderIM": "0", "totalPositionIM": "12.40",
                    "unrealisedPnl": "0.40",
                }],
            }]
        }

    def get_positions(self, **kwargs):
        return [{
            "symbol": "BTCUSDT", "side": "Buy", "size": "0.001",
            "avgPrice": "64000", "markPrice": "64400", "positionValue": "64.4",
            "leverage": "3", "unrealisedPnl": "0.40", "positionIM": "21.2",
            "stopLoss": "63000", "takeProfit": "66000",
        }]


def instrument():
    return {
        "symbol": "GPSUSDT",
        "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "305", "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "10", "leverageStep": "0.01"},
    }


def snapshot():
    return {
        "symbol": "GPSUSDT", "price": 0.0164, "captured_at_ms": 0,
        "spread_bps": 2.0,
    }


def assessment():
    return {
        "action": "long", "confidence": 0.80,
        "entry": 0.0164, "stop_loss": 0.0159, "take_profit": 0.017225,
    }


def main() -> None:
    from account_capacity import (
        unified_margin_state, live_position_inventory, pre_ai_capacity_gate,
        futures_minimum_notional, recent_capacity_reject_gate,
    )
    from risk_engine import evaluate_trade_candidate
    from trading_config import apply_safe_autopilot_profile
    from trading_usage import ensure_budget_epoch, ensure_budget_epoch_compatible, budget_epoch_status
    from research_store import set_research_state

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    set_research_state("bootstrap_complete", "1")
    assert int(cfg["autopilot_profile_version"]) == 20, cfg
    assert cfg["futures_capacity_pre_ai_enabled"] is True
    assert float(cfg["futures_available_balance_utilization_pct"]) == 82.0
    assert float(cfg["futures_available_balance_reserve_usdt"]) == 2.0
    assert int(cfg["futures_capacity_reject_cooldown_minutes"]) == 20
    # Capacity patch must not secretly raise the loss-at-stop envelope.
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert float(cfg["portfolio_absolute_risk_cap_usdt"]) == 20.0

    state = unified_margin_state(CapacityClient())
    assert state["total_available_balance_usd"] == 11.0, state
    assert state["total_initial_margin_usd"] == 12.4, state
    inv = live_position_inventory(CapacityClient())
    assert inv["count"] == 1 and inv["symbols"] == ["BTCUSDT"], inv
    assert inv["unprotected"] == [] and inv["positions"][0]["protected"] is True, inv

    min_notional = futures_minimum_notional(instrument(), snapshot()["price"])
    gate = pre_ai_capacity_gate(
        available_balance_usd=11.0,
        minimum_notional_usdt=min_notional,
        leverage_cap=3.0,
        utilization_pct=82.0,
        reserve_usdt=2.0,
    )
    assert gate["allowed"] is True, gate
    tiny = pre_ai_capacity_gate(
        available_balance_usd=2.1,
        minimum_notional_usdt=min_notional,
        leverage_cap=3.0,
        utilization_pct=82.0,
        reserve_usdt=2.0,
    )
    assert tiny["allowed"] is False, tiny

    reject_record = {
        "symbol": "GPSUSDT", "ts": 1000.0,
        "capacity": {"total_available_balance_usd": 10.0},
    }
    cooldown = recent_capacity_reject_gate(
        reject_record, symbol="GPSUSDT", current_available_balance_usd=10.5, now_ts=1060.0,
        cooldown_minutes=20, recovery_usdt=3.0,
    )
    assert cooldown["blocked"] is True, cooldown
    recovered = recent_capacity_reject_gate(
        reject_record, symbol="GPSUSDT", current_available_balance_usd=13.0, now_ts=1060.0,
        cooldown_minutes=20, recovery_usdt=3.0,
    )
    assert recovered["blocked"] is False and recovered.get("recovered") is True, recovered

    risk = evaluate_trade_candidate(
        assessment(), snapshot(), instrument(), equity=79.0,
        open_positions=1,
        daily_realized_pnl=-1.19, weekly_realized_pnl=-1.19,
        trades_today=2, learning_risk_multiplier=0.7,
        adaptive_risk_pct=3.0, leverage_cap=3.0,
        exposure_cap_pct=225.0, max_trades_today_allowed=10,
        max_positions_allowed=3, growth_stage="learning",
        performance_metrics={}, confidence_bump=0.0, safety_pause=False,
        learning_notes=[], portfolio_open_risk_pct=0.47,
        portfolio_risk_cap_pct=25.0, same_symbol_open=False,
        unprotected_positions=[], max_directional_correlation={},
        available_balance_usd=11.0,
    )
    assert risk["allowed"] is True, risk
    assert risk["margin_resized"] is True, risk
    assert float(risk["notional_usdt"]) <= float(risk["margin_notional_cap_usdt"]) + 0.1, risk
    assert float(risk["leverage"]) <= 3.0, risk
    assert float(risk["required_initial_margin_estimate_usd"]) <= float(risk["margin_budget_usd"]) + 1e-6, risk
    assert float(risk["actual_risk_per_trade_pct"]) <= 7.0, risk

    blocked = evaluate_trade_candidate(
        assessment(), snapshot(), instrument(), equity=79.0,
        open_positions=1,
        daily_realized_pnl=-1.19, weekly_realized_pnl=-1.19,
        trades_today=2, learning_risk_multiplier=0.7,
        adaptive_risk_pct=3.0, leverage_cap=3.0,
        exposure_cap_pct=225.0, max_trades_today_allowed=10,
        max_positions_allowed=3, growth_stage="learning",
        performance_metrics={}, confidence_bump=0.0, safety_pause=False,
        learning_notes=[], portfolio_open_risk_pct=0.47,
        portfolio_risk_cap_pct=25.0, same_symbol_open=False,
        unprotected_positions=[], max_directional_correlation={},
        available_balance_usd=2.1,
    )
    assert blocked["allowed"] is False, blocked

    # Same-day v4.6.5 budget must carry forward: installing a hotfix is not a token reset.
    first = ensure_budget_epoch("v4.6.5")
    carried = ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})
    assert carried["carried_forward"] is True and carried["baseline_rowid"] == first["baseline_rowid"], carried
    assert budget_epoch_status()["version"] == "v4.6.9"

    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    ui = (ROOT / "account_os_ui.py").read_text(encoding="utf-8")
    assert "live_position_inventory(live_state_client)" in engine
    assert "live Futures position already open on this symbol; paid AI skipped before verification" in engine
    assert "pre_ai_capacity_gate" in engine and "account capacity block" in engine
    assert "recent capacity-reject memory" in engine
    assert "current Bybit available balance could not be verified while an existing Futures position is open" in engine
    assert 'bump_proposal_stat("capacity_resized"' in engine
    assert 'bump_proposal_stat("capacity_rejected"' in engine
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account
    assert "LIVE FUTURES INVENTORY" in ui and "ACCOUNT CAPACITY" in ui

    print("v4.6.6 available-balance / restart reconciliation regression under v4.6.9: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
