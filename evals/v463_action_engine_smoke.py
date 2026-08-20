from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v463-action-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def _base_futures() -> dict:
    return {
        "symbol": "TESTUSDT", "interval": "15", "price": 100.0, "signal_price": 99.8,
        "atr14": 1.0, "atr_pct": 1.0, "spread_bps": 2.0, "setup_strength": 0.72,
        "directional_score": 0.62, "local_bias": "long", "return_4_pct": 1.2,
        "return_12_pct": 2.4, "vwap_distance_20_pct": 0.35, "range_position_20": 0.66,
        "breakout_20_atr": -0.15, "breakdown_20_atr": -3.0, "volume_ratio_20": 1.45,
        "volume_z_20": 1.0, "orderbook_imbalance_10": 0.24, "open_interest_change_pct": 2.0,
        "oi_price_regime": "price_up_oi_up", "rsi14": 61.0,
    }


def main() -> None:
    from trading_config import apply_safe_autopilot_profile
    import trading_usage as tu
    from trade_proposal import (
        build_futures_proposal, build_spot_proposal, record_proposal_veto,
        veto_blocks_proposal, clear_proposal_veto, choose_best_preflight_candidate,
    )

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) >= 14, cfg
    assert cfg["futures_news_fail_closed"] is False
    assert int(cfg["proposal_watchlist_preflight"]) == 4
    assert float(cfg["growth_calibration_risk_pct"]) == 3.0
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert float(cfg["portfolio_absolute_risk_cap_usdt"]) == 20.0

    # Proposal-aware watchlist preflight prefers executable/high-quality proposals over a
    # scanner winner that has no executable geometry, without any paid AI call.
    preflight_pick = choose_best_preflight_candidate([
        {"symbol": "EXTENDED", "scanner_score": 0.95, "proposal": {"eligible": False, "quality": 0.0}, "veto_blocked": False},
        {"symbol": "NORMAL", "scanner_score": 0.70, "proposal": {"eligible": True, "quality": 0.70, "priority": "normal"}, "veto_blocked": False},
        {"symbol": "HIGH", "scanner_score": 0.68, "proposal": {"eligible": True, "quality": 0.78, "priority": "high"}, "veto_blocked": False},
    ])
    assert preflight_pick and preflight_pick["symbol"] == "HIGH", preflight_pick

    # A balanced directional setup must become a concrete executable geometry before AI.
    snap = _base_futures()
    proposal = build_futures_proposal(snap, cfg, strategy_supported=False)
    assert proposal["eligible"] is True, proposal
    assert proposal["action"] == "long", proposal
    assert proposal["stop_loss"] < proposal["entry"] < proposal["take_profit"], proposal
    assert float(proposal["reward_risk"]) >= float(cfg["min_reward_risk"]), proposal

    # A model-approved deterministic proposal can actually pass the existing Risk Engine;
    # the new action path is not a display-only signal.
    from research_store import set_research_state
    from risk_engine import evaluate_trade_candidate
    set_research_state("bootstrap_complete", "1")
    assessment = {
        "action": proposal["action"], "confidence": 0.78, "entry": proposal["entry"],
        "stop_loss": proposal["stop_loss"], "take_profit": proposal["take_profit"],
        "analysis_symbol": snap["symbol"], "analysis_interval": snap["interval"],
    }
    risk_snapshot = dict(snap)
    risk_snapshot.update({"captured_at_ms": 0, "closed_candle_start_ms": 0})
    instrument = {
        "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01", "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "3", "leverageStep": "0.01"},
    }
    risk = evaluate_trade_candidate(
        assessment, risk_snapshot, instrument, equity=80.0, open_positions=0,
        daily_realized_pnl=0.0, weekly_realized_pnl=0.0, trades_today=0,
        adaptive_risk_pct=3.0, leverage_cap=2.0, exposure_cap_pct=125.0,
        max_trades_today_allowed=10, max_positions_allowed=3, growth_stage="learning",
        portfolio_open_risk_pct=0.0, portfolio_risk_cap_pct=25.0,
    )
    assert risk["allowed"] is True, risk
    assert risk["sizing_evaluated"] is True and float(risk["reward_risk"]) >= 1.3, risk

    # The exact late-stage HUSDT pattern observed live must be rejected locally instead of
    # consuming another paid HOLD call.
    extended = dict(snap)
    extended.update({
        "symbol": "HUSDT", "setup_strength": 0.7813, "directional_score": 1.0,
        "vwap_distance_20_pct": 9.2858, "atr_pct": 3.0739, "rsi14": 85.632,
        "range_position_20": 1.0989, "volume_ratio_20": 1.0376,
        "orderbook_imbalance_10": 0.9246, "oi_price_regime": "price_up_oi_down",
    })
    blocked = build_futures_proposal(extended, cfg, strategy_supported=False)
    assert blocked["eligible"] is False, blocked
    assert "extended" in blocked["reason"].lower() or "breakout" in blocked["reason"].lower(), blocked

    # HOLD/veto memory suppresses same-structure re-analysis but clears immediately when
    # the structural proposal signature changes.
    record_proposal_veto("TESTUSDT", "15", signature=proposal["signature"], reason="test veto", action="long", minutes=90, lane="futures")
    blocked_same, _ = veto_blocks_proposal(proposal, "15", "futures")
    assert blocked_same is True
    changed = dict(proposal)
    changed["signature"] = proposal["signature"] + ":changed"
    blocked_changed, _ = veto_blocks_proposal(changed, "15", "futures")
    assert blocked_changed is False
    clear_proposal_veto("TESTUSDT", "15", "futures")

    # Normal entry-verification budget cannot consume the high-priority reserve lane.
    tu.ensure_budget_epoch("v4.6.4")
    for _ in range(int(cfg["futures_entry_verify_calls_daily"])):
        tu.record_trading_tokens(1000, kind="futures_entry_verify")
    allowed, reason = tu.reserve_ai_call(
        "futures_entry_verify",
        kind_budget=int(cfg["futures_entry_verify_tokens_daily"]),
        kind_max_calls=int(cfg["futures_entry_verify_calls_daily"]),
        estimated_tokens=1000,
        cooldown_key="normal-lane-test",
        signature="normal",
    )
    assert allowed is False and "call cap" in reason.lower(), reason
    allowed_reserve, reason_reserve = tu.reserve_ai_call(
        "futures_entry_reserve",
        kind_budget=int(cfg["futures_entry_reserve_tokens_daily"]),
        kind_max_calls=int(cfg["futures_entry_reserve_calls_daily"]),
        estimated_tokens=1000,
        cooldown_key="reserve-lane-test",
        signature="high-priority",
    )
    assert allowed_reserve is True, reason_reserve

    # OOS-supported Spot can enter the proposal funnel at a lower local setup threshold,
    # while still requiring non-extended geometry and positive participation.
    spot = {
        "symbol": "HUSDT", "interval": "15", "price": 0.11, "atr14": 0.002,
        "atr_pct": 1.8, "spread_bps": 8.0, "setup_strength": 0.62,
        "local_bias": "buy_candidate", "vwap_distance_20_pct": 0.4, "range_position_20": 0.58,
        "return_4_pct": 1.0, "return_12_pct": 2.0, "volume_ratio_20": 1.35,
        "orderbook_imbalance_10": 0.20,
    }
    spot_prop = build_spot_proposal(spot, cfg, strategy_supported=True)
    assert spot_prop["eligible"] is True, spot_prop
    assert 0 < spot_prop["stop_loss"] < spot_prop["entry"] < spot_prop["take_profit"], spot_prop

    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    ai = (ROOT / "trading_ai.py").read_text(encoding="utf-8")
    spot_engine = (ROOT / "spot_engine.py").read_text(encoding="utf-8")
    ui = (ROOT / "account_os_ui.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    assert "build_futures_proposal" in engine
    assert "proposal_preflight" in engine and "choose_best_preflight_candidate" in engine
    assert "futures_entry_reserve" in engine and "futures_entry_verify" in engine
    assert "proposal_verdict" in engine
    assert "safety verification" in ai or "safety verifier" in ai
    assert "build_spot_proposal" in spot_engine
    assert "PROPOSAL → AI VERIFY/REUSE → RISK → SUBMIT → CONFIRMED FILL" in ui
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account
    assert "news_verification_skipped" in engine
    assert "futures_news_fail_closed" in engine

    print("v4.6.3 action engine regression under v4.6.9: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)

