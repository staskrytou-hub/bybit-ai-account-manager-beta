from __future__ import annotations

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_config import apply_safe_autopilot_profile, audit_autopilot_growth_profile
from research_store import set_research_state
from risk_engine import evaluate_trade_candidate
from live_learning import _performance_metrics, _growth_stage


def _rows(wins: int, losses: int, win: float = 1.0, loss: float = -0.5) -> list[dict]:
    rows=[]
    t=1700000000000
    for _ in range(wins):
        rows.append({"closedPnl": str(win), "updatedTime": str(t)}); t += 1
    for _ in range(losses):
        rows.append({"closedPnl": str(loss), "updatedTime": str(t)}); t += 1
    return rows


def main() -> None:
    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    audit = audit_autopilot_growth_profile(cfg)
    assert audit["passed"], audit
    assert cfg["absolute_risk_cap_pct"] == 7.0
    assert cfg["growth_learning_risk_pct"] < cfg["growth_validated_risk_pct"] < cfg["growth_mature_risk_pct"] <= cfg["absolute_risk_cap_pct"]

    # Make this smoke hermetic even if the same STAN_AI_HOME was used before.
    set_research_state("bootstrap_complete", "0")

    snap = {"price": 100.0, "spread_bps": 2.0, "captured_at_ms": 0, "symbol": "TESTUSDT"}
    assessment = {"action": "long", "confidence": 0.80, "entry": 100.0, "stop_loss": 99.5, "take_profit": 101.0}
    instrument = {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}, "leverageFilter": {"maxLeverage": "50", "leverageStep": "0.01"}}

    blocked = evaluate_trade_candidate(assessment, snap, instrument, equity=90.0, adaptive_risk_pct=1.0, leverage_cap=2.0, exposure_cap_pct=125.0, growth_stage="learning")
    assert not blocked["allowed"]
    assert any("bootstrap" in reason for reason in blocked["reasons"])

    set_research_state("bootstrap_complete", "1")
    learning = evaluate_trade_candidate(
        assessment, snap, instrument, equity=90.0, open_positions=0,
        daily_realized_pnl=0.0, weekly_realized_pnl=0.0, trades_today=0,
        adaptive_risk_pct=1.0, leverage_cap=2.0, exposure_cap_pct=125.0,
        max_trades_today_allowed=10, growth_stage="learning",
    )
    assert learning["allowed"], learning
    assert learning["effective_risk_per_trade_pct"] <= 1.0
    assert learning["notional_usdt"] <= 112.5 + 1e-9
    assert learning["leverage"] <= 2.0

    # Prove that live sizing no longer has the old fixed $50 ceiling.
    validated = evaluate_trade_candidate(
        assessment, snap, instrument, equity=200.0, open_positions=0,
        daily_realized_pnl=0.0, weekly_realized_pnl=1.0, trades_today=0,
        adaptive_risk_pct=2.0, leverage_cap=2.5, exposure_cap_pct=175.0,
        max_trades_today_allowed=12, growth_stage="validated",
    )
    assert validated["allowed"], validated
    assert validated["notional_usdt"] > 50.0, validated
    assert validated["notional_cap_usdt"] == 350.0

    # Even a bad adaptive input can never exceed the absolute hard risk cap.
    hard = evaluate_trade_candidate(
        assessment, snap, instrument, equity=200.0, open_positions=0,
        daily_realized_pnl=0.0, weekly_realized_pnl=1.0, trades_today=0,
        adaptive_risk_pct=10.0, leverage_cap=10.0, exposure_cap_pct=300.0,
        max_trades_today_allowed=16, growth_stage="mature",
    )
    assert hard["adaptive_base_risk_pct"] <= cfg["absolute_risk_cap_pct"]
    assert hard["leverage_cap"] <= cfg["max_leverage"]

    m1=_performance_metrics(_rows(30,10), 100.0)
    assert _growth_stage(m1, 5.0, 0, cfg) == "validated"
    m2=_performance_metrics(_rows(80,20, win=1.0, loss=-0.2), 100.0)
    assert _growth_stage(m2, 5.0, 0, cfg) == "mature"

    too_many = evaluate_trade_candidate(
        assessment, snap, instrument, equity=100.0, open_positions=0,
        daily_realized_pnl=0.0, weekly_realized_pnl=0.0, trades_today=10,
        adaptive_risk_pct=1.5, leverage_cap=2.0, exposure_cap_pct=125.0,
        max_trades_today_allowed=10, growth_stage="learning",
    )
    assert not too_many["allowed"]
    assert any("growth-stage trade limit" in reason for reason in too_many["reasons"])

    print("autopilot adaptive-growth smoke: PASS")


if __name__ == "__main__":
    main()
