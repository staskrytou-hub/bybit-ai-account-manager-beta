from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v44-stop-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    from runtime_control import clear_manual_stop, manual_stop_active, request_manual_stop
    import core_client
    from trading_config import apply_safe_autopilot_profile
    from research_store import set_research_state
    from risk_engine import evaluate_trade_candidate
    from strategy_governor import build_strategy_governor, strategy_support
    from adaptive_strategy_lab import ALLOWED_FEATURES

    clear_manual_stop()
    assert not manual_stop_active()
    request_manual_stop("eval STOP")
    assert manual_stop_active()

    # Status polling must NOT clear or bypass a user STOP and must not auto-launch Core.
    old_health = core_client.health
    old_ensure = core_client.ensure_running
    try:
        core_client.health = lambda: False
        core_client.ensure_running = lambda *a, **k: (_ for _ in ()).throw(AssertionError("status polling tried to restart Core"))
        st = core_client.status()
        assert st.get("manual_stop", {}).get("active") is True, st
        assert st.get("running") is False, st
    finally:
        core_client.health = old_health
        core_client.ensure_running = old_ensure
    clear_manual_stop()

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    set_research_state("bootstrap_complete", "1")
    assessment = {"action": "long", "confidence": 0.90, "entry": 1.0, "stop_loss": 0.99, "take_profit": 1.03}
    snapshot = {"symbol": "TESTUSDT", "price": 1.0, "spread_bps": 1.0, "captured_at_ms": 0}

    # Target size falls below Bybit minimum, but the minimum order still stays inside the hard risk envelope.
    instrument_safe = {
        "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "20", "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "10", "leverageStep": "0.01"},
    }
    safe = evaluate_trade_candidate(
        assessment, snapshot, instrument_safe, equity=86.0, open_positions=0,
        adaptive_risk_pct=0.15, leverage_cap=2.0, exposure_cap_pct=75.0,
        max_trades_today_allowed=8, growth_stage="learning",
    )
    assert safe["allowed"], safe
    assert safe["min_order_override_used"], safe
    assert safe["qty"] == 20.0, safe
    assert safe["actual_risk_per_trade_pct"] <= cfg["min_order_override_max_risk_pct"] + 1e-9, safe

    # A minimum executable size that would breach hard risk remains blocked.
    instrument_unsafe = {
        "lotSizeFilter": {"qtyStep": "1", "minOrderQty": "300", "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "10", "leverageStep": "0.01"},
    }
    unsafe = evaluate_trade_candidate(
        assessment, snapshot, instrument_unsafe, equity=86.0, open_positions=0,
        adaptive_risk_pct=0.15, leverage_cap=2.0, exposure_cap_pct=75.0,
        max_trades_today_allowed=8, growth_stage="learning",
    )
    assert not unsafe["allowed"], unsafe
    assert any("minimum" in x.lower() for x in unsafe["reasons"]), unsafe

    # v4.4 adaptive research is not tied to EMA/RSI textbook rules.
    assert not any("ema" in x.lower() or "rsi" in x.lower() for x in ALLOWED_FEATURES), ALLOWED_FEATURES
    legacy = {
        "symbol": "BTCUSDT", "interval": "15", "strategy": "legacy", "name": "Legacy benchmark",
        "strategy_family": "benchmark_legacy", "adaptive": False, "robust": True, "robustness_score": 0.5,
        "out_of_sample": {"expectancy_r": 0.2, "profit_factor": 1.4, "trades": 20},
    }
    adaptive = {
        "symbol": "BTCUSDT", "interval": "15", "strategy": "adaptive_now", "name": "Current regime",
        "strategy_family": "adaptive_current_regime", "adaptive": True, "robust": True, "robustness_score": 0.4,
        "out_of_sample": {"expectancy_r": 0.15, "profit_factor": 1.3, "trades": 18},
    }
    gov = build_strategy_governor([legacy, adaptive])
    assert gov["approved_count"] == 1, gov
    assert gov["approved"][0]["strategy"] == "adaptive_now", gov
    assert strategy_support("BTCUSDT", "15")["supported"] is True

    print("v4.4 strict-stop / executable-minimum / adaptive-strategy smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
