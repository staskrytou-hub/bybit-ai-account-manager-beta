from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v461-efficiency-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))

try:
    import agents  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    import types
    agents_stub = types.ModuleType("agents")
    class _RunnerStub:
        @staticmethod
        def run_sync(*args, **kwargs):
            raise AssertionError("Runner stub must be monkeypatched")
    agents_stub.Runner = _RunnerStub
    sys.modules["agents"] = agents_stub


def main() -> None:
    import runtime_control as rc
    import resilience
    import trading_usage as tu
    from trading_config import apply_safe_autopilot_profile

    original_runner = resilience.Runner.run_sync
    original_stop_file = rc.MANUAL_STOP_FILE
    try:
        rc.MANUAL_STOP_FILE = TEMP / "data" / "stan_manual_stop.json"
        rc.clear_manual_stop()
        rc.begin_runtime_generation()
        tu.clear_provider_guard()

        calls = {"n": 0}

        class CreditError(RuntimeError):
            status_code = 429

        def no_credit(*args, **kwargs):
            calls["n"] += 1
            raise CreditError("429 {'error': {'type':'insufficient_quota','code':'credit_balance_exhausted','message':'You have no credits remaining'}}")

        resilience.Runner.run_sync = no_credit
        try:
            resilience.run_sync_resilient(object(), "x", kind="trading.analysis")
        except resilience.ProviderCreditExhaustedError:
            pass
        else:
            raise AssertionError("credit exhaustion must surface as ProviderCreditExhaustedError")
        assert calls["n"] == 1, calls  # permanent billing 429 must never retry
        guard = tu.provider_guard_status()
        assert guard["paused"] is True and guard["code"] == "credit_balance_exhausted", guard

        # Circuit open: no provider request is sent by another autonomous AI path.
        try:
            resilience.run_sync_resilient(object(), "x", kind="trading.spot_analysis")
        except resilience.AIProviderPausedError:
            pass
        else:
            raise AssertionError("open provider circuit must block new autonomous model calls")
        assert calls["n"] == 1, calls

        # User-forced recovery probe can test a replenished account immediately.
        tu.request_provider_probe()
        resilience.Runner.run_sync = lambda *a, **k: {"ok": True}
        value = resilience.run_sync_resilient(object(), "x", kind="trading.analysis")
        assert value == {"ok": True}
        assert tu.provider_guard_status()["state"] == "ACTIVE", tu.provider_guard_status()

        # Per-kind spend reserve prevents another paid call before it begins.
        tu.record_trading_tokens(49_000, kind="futures_decision")
        allowed, reason = tu.reserve_ai_call(
            "futures_decision", kind_budget=50_000, kind_max_calls=18,
            estimated_tokens=3_800, cooldown_key="budget-test", signature="new-evidence",
        )
        assert allowed is False and "budget" in reason.lower(), (allowed, reason)

        cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
        assert int(cfg["autopilot_profile_version"]) >= 12, cfg
        assert int(cfg["ai_heartbeat_candles"]) == 0, cfg
        assert float(cfg["ai_candidate_threshold"]) >= 0.68, cfg
        assert isinstance(cfg["ai_rotation_only_strong"], bool), cfg
        assert bool(cfg["promotion_auto_ai_refresh_enabled"]) is False, cfg
        assert int(cfg["futures_ai_calls_daily"]) == 18, cfg
        assert int(cfg["spot_ai_calls_daily"]) == 10, cfg

        # Spot hard execution gates are source-checked here; importing the full
        # Spot agent stack would require optional provider packages in this local smoke test.

        engine_text = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
        spot_text = (ROOT / "spot_engine.py").read_text(encoding="utf-8-sig")
        promo_text = (ROOT / "promotion_ai.py").read_text(encoding="utf-8")
        ui_text = (ROOT / "account_os_ui.py").read_text(encoding="utf-8")
        assert "build_futures_proposal" in engine_text
        assert "deterministic pre-AI block" in engine_text
        assert "futures_news_verify" in engine_text
        assert "action_now in {\"long\", \"short\"}" in engine_text
        assert "build_spot_proposal" in spot_text
        assert "Only a preliminary BUY can justify" in spot_text
        assert "automatic paid Promotion Intelligence is disabled" in promo_text or "Automatic paid Promotion Intelligence is disabled" in promo_text
        assert "AI PAUSED — API CREDIT EXHAUSTED" in ui_text

        print("v4.6.1 token efficiency / provider circuit breaker / trade funnel smoke: PASS")
    finally:
        resilience.Runner.run_sync = original_runner
        rc.MANUAL_STOP_FILE = original_stop_file
        rc.clear_manual_stop()
        shutil.rmtree(TEMP, ignore_errors=True)


if __name__ == "__main__":
    main()
