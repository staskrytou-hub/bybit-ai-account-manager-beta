from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="stan-v456-promo-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP_ROOT)
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
    import promotion_lifecycle as pl

    original_stop_file = rc.MANUAL_STOP_FILE
    original_runner = resilience.Runner.run_sync
    original_get = pl.get_state
    original_set = pl.set_state
    try:
        with tempfile.TemporaryDirectory() as td:
            rc.MANUAL_STOP_FILE = Path(td) / "manual_stop.json"
            rc.clear_manual_stop()
            rc.begin_runtime_generation()

            caller_thread = threading.get_ident()
            seen: dict[str, int] = {}

            def fake_runner(agent, input_value, *, session=None, max_turns=10):
                seen["thread"] = threading.get_ident()
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("isolated Runner still has a running asyncio loop")
                return {"ok": True}

            resilience.Runner.run_sync = fake_runner

            async def invoke_from_running_loop():
                return resilience.run_sync_resilient_isolated(
                    object(), "x", kind="trading.promotion_action", max_turns=1
                )

            result = asyncio.run(invoke_from_running_loop())
            assert result == {"ok": True}
            assert seen.get("thread") and seen["thread"] != caller_thread

            store: dict[str, object] = {}
            pl.get_state = lambda key, default=None: store.get(key, default)
            pl.set_state = lambda key, value: store.__setitem__(key, value)
            campaign = {"campaign_key": "stale-action", "name": "Stale Action"}
            pl.update_lifecycle(
                campaign, "ACTION_RUNNING", evidence="before click", action="Join",
                extra={"last_attempt_at": "2020-01-01T00:00:00+00:00"},
            )
            recovered = pl.recover_stale_action_running(campaign, stale_seconds=60)
            assert recovered["state"] == "RETRY_WAIT"
            assert recovered["last_attempt_at"] == "2020-01-01T00:00:00+00:00"

        print("v4.5.6 promotion verifier isolation/lifecycle smoke: PASS")
    finally:
        rc.MANUAL_STOP_FILE = original_stop_file
        resilience.Runner.run_sync = original_runner
        pl.get_state = original_get
        pl.set_state = original_set


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
