from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v462-activation-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))

# Minimal Agents SDK stub for source-level/provider-circuit tests in environments
# where openai-agents is not installed.
agents_stub = types.ModuleType("agents")
class _RunnerStub:
    @staticmethod
    def run_sync(*args, **kwargs):
        return object()
class _AgentStub:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
class _ModelSettingsStub:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
agents_stub.Runner = _RunnerStub
agents_stub.Agent = _AgentStub
agents_stub.ModelSettings = _ModelSettingsStub
sys.modules.setdefault("agents", agents_stub)


def main() -> None:
    import runtime_control as rc
    import resilience
    import trading_usage as tu
    import provider_probe
    from trading_config import apply_safe_autopilot_profile, audit_autopilot_growth_profile
    from opportunity_manager import build_opportunity_plan

    original_runner = resilience.Runner.run_sync
    original_usage_summary = provider_probe._usage_summary
    original_stop_file = rc.MANUAL_STOP_FILE
    try:
        rc.MANUAL_STOP_FILE = TEMP / "data" / "stan_manual_stop.json"
        rc.clear_manual_stop()
        rc.begin_runtime_generation()
        tu.clear_provider_guard()

        # Same-day v4.6.1 usage must remain visible historically but not consume the
        # freshly-installed v4.6.2 per-kind budget.
        for _ in range(12):
            tu.record_trading_tokens(1000, kind="spot_decision")
        assert tu.trading_ai_calls_today("spot_decision") == 12
        epoch = tu.ensure_budget_epoch("v4.6.2")
        assert epoch["version"] == "v4.6.2"
        assert tu.budgeted_ai_calls_today("spot_decision") == 0
        allowed, reason = tu.reserve_ai_call(
            "spot_decision", kind_budget=28_000, kind_max_calls=10,
            estimated_tokens=3400, cooldown_key="v462-budget", signature="fresh-evidence",
        )
        assert allowed is True, reason

        # Dedicated provider probe is independent of market thresholds and clears the
        # circuit on one successful tiny provider call.
        tu.trip_provider_guard(code="credit_balance_exhausted", reason="test", error="no credits", probe_after_seconds=900)
        provider_probe._usage_summary = lambda result: {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        resilience.Runner.run_sync = lambda *a, **k: object()
        result = provider_probe.run_provider_probe(force=True)
        assert result["recovered"] is True, result
        assert tu.provider_guard_status()["state"] == "ACTIVE", tu.provider_guard_status()
        assert tu.trading_ai_calls_today("provider_probe") == 1

        cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
        assert int(cfg["autopilot_profile_version"]) >= 13, cfg
        assert abs(float(cfg["growth_calibration_risk_pct"]) - 3.0) < 1e-9, cfg
        assert abs(float(cfg["growth_learning_risk_pct"]) - 4.0) < 1e-9, cfg
        assert abs(float(cfg["growth_validated_risk_pct"]) - 5.0) < 1e-9, cfg
        assert abs(float(cfg["growth_mature_risk_pct"]) - 6.0) < 1e-9, cfg
        assert abs(float(cfg["absolute_risk_cap_pct"]) - 7.0) < 1e-9, cfg
        assert abs(float(cfg["portfolio_absolute_risk_cap_usdt"]) - 20.0) < 1e-9, cfg
        assert abs(float(cfg["portfolio_absolute_risk_cap_pct"]) - 25.0) < 1e-9, cfg
        assert float(cfg["ai_candidate_threshold"]) == 0.68, cfg
        assert float(cfg["ai_strong_candidate_threshold"]) == 0.80, cfg
        assert bool(cfg["ai_rotation_only_strong"]) is False, cfg
        assert float(cfg["spot_ai_candidate_threshold"]) == 0.72, cfg
        assert float(cfg["spot_ai_strong_threshold"]) == 0.82, cfg
        audit = audit_autopilot_growth_profile(cfg)
        assert audit["passed"] is True, audit

        # Explicitly ineligible/region-restricted campaigns must remain tracking-only;
        # safe account-specific Rewards Hub actions are handled separately by the browser.
        plan = build_opportunity_plan({"campaigns": [{
            "campaign_key": "blocked", "name": "Blocked", "requires_registration": True,
            "actionability": "not_actionable_region_restricted", "source_url": "https://www.bybit.com/x",
        }]}, equity_usdt=86.47)
        assert not plan["human_action_required"], plan
        assert len(plan["tracked"]) == 1, plan

        account_text = (ROOT / "account_os.py").read_text(encoding="utf-8")
        probe_text = (ROOT / "provider_probe.py").read_text(encoding="utf-8")
        risk_text = (ROOT / "risk_engine.py").read_text(encoding="utf-8")
        assert "run_provider_probe(force=True)" in account_text
        assert "run_provider_probe_async" in account_text
        assert "trading.provider_probe" in probe_text
        assert "portfolio_absolute_risk_cap_usdt" in risk_text

        print("v4.6.2 activation recovery / budget epoch / aggressive risk smoke: PASS")
    finally:
        resilience.Runner.run_sync = original_runner
        provider_probe._usage_summary = original_usage_summary
        rc.MANUAL_STOP_FILE = original_stop_file
        rc.clear_manual_stop()
        shutil.rmtree(TEMP, ignore_errors=True)


if __name__ == "__main__":
    main()
