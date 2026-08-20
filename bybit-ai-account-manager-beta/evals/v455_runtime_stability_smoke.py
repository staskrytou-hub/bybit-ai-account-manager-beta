from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="stan-v455-runtime-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP_ROOT)
sys.path.insert(0, str(ROOT))

# The delivery sandbox may not have openai-agents installed. Stub only the Runner
# surface needed by this deterministic resilience smoke; Windows build installs the real package.
try:
    import agents  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    import types

    agents_stub = types.ModuleType("agents")

    class _RunnerStub:
        @staticmethod
        def run_sync(*args, **kwargs):
            raise AssertionError("Runner stub must be monkeypatched by the smoke test")

    agents_stub.Runner = _RunnerStub
    sys.modules["agents"] = agents_stub


def main() -> None:
    import runtime_control as rc
    import resilience
    import promotion_lifecycle as pl
    from browser_operator import BybitBrowserOperator, _target_closed_error
    from trading_config import resolve_execution_environment

    original_stop_file = rc.MANUAL_STOP_FILE
    original_runner = resilience.Runner.run_sync
    original_wait = resilience.interruptible_wait
    original_get = pl.get_state
    original_set = pl.set_state

    try:
        with tempfile.TemporaryDirectory() as td:
            rc.MANUAL_STOP_FILE = Path(td) / "manual_stop.json"
            rc.clear_manual_stop()
            generation = rc.begin_runtime_generation()
            assert generation
            assert rc.runtime_snapshot()["state"] == "RUNNING"

            calls = {"n": 0}

            def fail_retryable(agent, input_value, *, session=None, max_turns=10):
                calls["n"] += 1
                raise TimeoutError("simulated provider timeout")

            resilience.Runner.run_sync = fail_retryable
            resilience.interruptible_wait = lambda seconds, step=0.1: True
            try:
                resilience.run_sync_resilient(object(), "x", kind="trading.analysis")
            except TimeoutError:
                pass
            else:
                raise AssertionError("retryable model error should surface after controlled retry")
            assert calls["n"] == 2, f"governed model path used {calls['n']} attempts, expected exactly 2"

            rc.request_runtime_stop("smoke stop")
            before = calls["n"]
            try:
                resilience.run_sync_resilient(object(), "x", kind="trading.analysis")
            except rc.RuntimeStoppedError:
                pass
            else:
                raise AssertionError("STOP must block a new autonomous model request")
            assert calls["n"] == before, "model provider was called after STOP"

            # Persistent manual STOP is authoritative across process boundaries.
            rc.request_manual_stop("smoke persistent stop")
            assert rc.manual_stop_active()
            rc.clear_manual_stop()
            rc.begin_runtime_generation()

        # Promotion lifecycle: a sent click is not silently turned back into ELIGIBLE and re-clicked.
        store: dict[str, object] = {}
        pl.get_state = lambda key, default=None: store.get(key, default)
        pl.set_state = lambda key, value: store.__setitem__(key, value)
        campaign = {"campaign_key": "smoke-promo", "name": "Smoke Promo", "source_url": "https://www.bybit.com/en/rewards_hub"}
        pl.update_lifecycle(campaign, "ELIGIBLE", evidence="auth ok")
        pl.update_lifecycle(campaign, "ACTION_SENT_UNVERIFIED", evidence="clicked", action="Register", extra={"last_attempt_at": pl._now()})
        state = pl.update_lifecycle(campaign, "DISCOVERED", evidence="rediscovered")
        assert state["state"] == "ACTION_SENT_UNVERIFIED"
        state = pl.update_lifecycle(campaign, "ELIGIBLE", evidence="page accessible")
        assert state["state"] == "ACTION_SENT_UNVERIFIED"
        allowed, _ = pl.action_retry_allowed(campaign, cooldown_seconds=3600)
        assert allowed is False

        # Stale AUTH_REQUIRED is recoverable after a real authenticated page becomes accessible.
        auth_campaign = {"campaign_key": "rewards-hub-auth", "name": "Bybit Rewards Hub", "source_url": "https://www.bybit.com/en/rewards_hub"}
        pl.update_lifecycle(auth_campaign, "AUTH_REQUIRED", evidence="login shown")
        resolved = pl.resolve_auth_required(auth_campaign, url="https://www.bybit.com/en/rewards_hub")
        assert resolved["state"] == "ELIGIBLE"

        # Browser action verification must distinguish an unverified click from completion.
        before_page = {"safe_action_candidates": [{"text": "Register Now"}], "text": "Register Now"}
        after_page = {"safe_action_candidates": [{"text": "Register Now"}], "text": "Campaign page"}
        inferred = BybitBrowserOperator.infer_action_state(before_page, after_page, "Register Now")
        assert inferred["state"] == "ACTION_SENT_UNVERIFIED" and not inferred["verified"]
        assert _target_closed_error(RuntimeError("Target page, context or browser has been closed"))
        assert _target_closed_error(RuntimeError("Protocol error (Target.createTarget): Failed to open a new tab"))

        # Canonical execution environment never depends on the legacy execution_testnet boolean.
        assert resolve_execution_environment(mode="autopilot_live", key_environment="mainnet_trade", configured="auto") == "mainnet"
        assert resolve_execution_environment(mode="testnet", key_environment="testnet", configured="auto") == "testnet"
        # Explicit conflicting config remains visible so prelaunch can hard-block it instead of silently guessing.
        assert resolve_execution_environment(mode="autopilot_live", key_environment="mainnet_trade", configured="testnet") == "testnet"

        print("v4.5.5 runtime/AI/browser/environment stability smoke: PASS")
    finally:
        rc.MANUAL_STOP_FILE = original_stop_file
        resilience.Runner.run_sync = original_runner
        resilience.interruptible_wait = original_wait
        pl.get_state = original_get
        pl.set_state = original_set


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
