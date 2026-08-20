from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v457-risk-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    from trading_config import apply_safe_autopilot_profile, audit_autopilot_growth_profile, save_trading_settings
    from research_store import set_research_state
    from risk_engine import evaluate_trade_candidate

    # Simulate an installed v4.5.6 profile: explicit START in v4.5.7 must migrate it.
    save_trading_settings({
        "autopilot_profile_version": 10,
        "growth_calibration_risk_pct": 0.15,
        "growth_learning_risk_pct": 0.25,
        "portfolio_learning_risk_cap_pct": 0.75,
        "absolute_risk_cap_pct": 0.75,
    })
    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) >= 13, cfg
    assert cfg["growth_calibration_risk_pct"] == 3.0, cfg
    assert cfg["growth_learning_risk_pct"] == 4.0, cfg
    assert cfg["growth_validated_risk_pct"] == 5.0, cfg
    assert cfg["growth_mature_risk_pct"] == 6.0, cfg
    assert cfg["absolute_risk_cap_pct"] == 7.0, cfg
    assert cfg["portfolio_learning_risk_cap_pct"] == 25.0, cfg
    assert cfg["portfolio_validated_risk_cap_pct"] == 25.0, cfg
    assert cfg["portfolio_mature_risk_cap_pct"] == 25.0, cfg
    assert cfg["portfolio_absolute_risk_cap_pct"] == 25.0, cfg
    assert cfg["max_daily_loss_pct"] == 25.0, cfg
    assert cfg["max_weekly_loss_pct"] == 30.0, cfg
    assert cfg["spot_absolute_risk_cap_pct"] == 2.00, cfg
    assert cfg["portfolio_absolute_risk_cap_usdt"] == 20.0, cfg
    assert audit_autopilot_growth_profile(cfg)["passed"] is True

    set_research_state("bootstrap_complete", "1")
    snap = {"symbol": "MICROUSDT", "price": 1.0, "spread_bps": 1.0, "captured_at_ms": 0}
    instrument = {
        "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "100", "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "10", "leverageStep": "0.01"},
    }

    # HOLD must not produce fake minimum-order/sizing errors.
    hold = evaluate_trade_candidate(
        {"action": "hold", "confidence": 0.78, "entry": 1.0, "stop_loss": 0.0, "take_profit": 0.0},
        snap, instrument, equity=86.0, adaptive_risk_pct=1.0, leverage_cap=2.0,
        exposure_cap_pct=125.0, max_positions_allowed=3, portfolio_risk_cap_pct=10.0,
        growth_stage="learning",
    )
    assert hold["allowed"] is False, hold
    assert hold["sizing_evaluated"] is False, hold
    assert not any("minimum" in x.lower() for x in hold["reasons"]), hold

    # A micro-account minimum order may be allowed when the actual stop-risk is
    # inside the explicit 2.5% minimum-order override and portfolio envelope.
    long = evaluate_trade_candidate(
        {"action": "long", "confidence": 0.90, "entry": 1.0, "stop_loss": 0.99, "take_profit": 1.03},
        snap, instrument, equity=86.0, adaptive_risk_pct=1.0, leverage_cap=2.0,
        exposure_cap_pct=125.0, max_positions_allowed=3, portfolio_risk_cap_pct=10.0,
        growth_stage="learning",
    )
    assert long["allowed"] is True, long
    assert long["min_order_override_used"] is True, long
    assert 1.0 < float(long["actual_risk_per_trade_pct"]) <= 3.5, long

    # Aggregate stop-risk still blocks entries once the stage portfolio budget is exceeded.
    blocked = evaluate_trade_candidate(
        {"action": "long", "confidence": 0.90, "entry": 1.0, "stop_loss": 0.99, "take_profit": 1.03},
        snap, instrument, equity=86.0, adaptive_risk_pct=1.0, leverage_cap=2.0,
        exposure_cap_pct=125.0, max_positions_allowed=3, portfolio_open_risk_pct=9.5,
        portfolio_risk_cap_pct=10.0, growth_stage="learning",
    )
    assert blocked["allowed"] is False, blocked
    assert any("portfolio risk" in x.lower() for x in blocked["reasons"]), blocked

    print("v4.5.7 aggressive micro-account risk / HOLD sizing smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
