from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v451-eval-"))
sys.path.insert(0, str(ROOT))


def main() -> None:
    import browser_operator as bo
    import trading_usage as tu

    # Browser discovery still works for a per-user Chrome install without Edge.
    local = TEMP / "LocalAppData"
    chrome = local / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.write_bytes(b"fake")
    old_env = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "USERPROFILE")}
    try:
        os.environ["LOCALAPPDATA"] = str(local)
        os.environ["USERPROFILE"] = str(TEMP / "User")
        os.environ["PROGRAMFILES"] = str(TEMP / "PF")
        os.environ["PROGRAMFILES(X86)"] = str(TEMP / "PF86")
        os.environ["PROGRAMW6432"] = str(TEMP / "PF")
        found = bo._find_browser(force_windows=True)
        assert found and found["name"] == "Google Chrome", found
        assert Path(found["path"]) == chrome, found
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # Isolate token governor DB.
    db = TEMP / "governor.db"
    old_db = tu.TRADING_DB
    tu.TRADING_DB = db
    try:
        ok, reason = tu.reserve_ai_call(
            "futures_decision", budget=90000, estimated_tokens=4000, max_calls=24,
            cooldown_key="futures:BTCUSDT:15", cooldown_seconds=2700, signature="sig-a",
        )
        assert ok, reason
        tu.record_trading_tokens(3500, kind="futures_decision")
        ok2, reason2 = tu.reserve_ai_call(
            "futures_decision", budget=90000, estimated_tokens=4000, max_calls=24,
            cooldown_key="futures:BTCUSDT:15", cooldown_seconds=2700, signature="sig-a",
        )
        assert not ok2 and "cooldown" in reason2.lower() or "same evidence" in reason2.lower(), reason2
        status = tu.ai_budget_status(budget=90000, max_calls=24)
        assert status["used_tokens"] == 3500, status
        assert status["calls"] == 1, status
        assert status["by_kind"]["futures_decision"]["tokens"] == 3500, status

        tu.record_trading_tokens(87000, kind="research_chief")
        ok3, reason3 = tu.reserve_ai_call(
            "spot_decision", budget=90000, estimated_tokens=1000, max_calls=24,
            cooldown_key="spot:BTCUSDT:15", cooldown_seconds=1, signature="sig-b",
        )
        assert not ok3 and "budget" in reason3.lower(), reason3
    finally:
        tu.TRADING_DB = old_db

    print("v4.5.1 browser + token governor smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
