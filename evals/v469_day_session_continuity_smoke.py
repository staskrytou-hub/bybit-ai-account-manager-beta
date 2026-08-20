from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v469-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


class FakeClosedPnlClient:
    def __init__(self, loss: float = -0.5, count: int = 10):
        self.rows = [
            {"closedPnl": str(loss), "updatedTime": str(1787200000000 + i * 60000)}
            for i in range(count)
        ]

    def get_closed_pnl(self, **kwargs):
        return list(self.rows)


def main() -> None:
    from trading_config import apply_safe_autopilot_profile
    from trading_usage import session_priority_paced_call_cap, session_opportunity_aware_paced_call_cap, ensure_budget_epoch, ensure_budget_epoch_compatible, budget_epoch_status
    from live_learning import live_learning_snapshot

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) == 20, cfg
    assert cfg["loss_streak_time_pause_enabled"] is False
    # User-requested trading risk / concurrency profile is preserved, not replaced by a one-position recovery mode.
    assert float(cfg["growth_learning_risk_pct"]) == 4.0
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert int(cfg["growth_learning_max_positions"]) == 3
    assert int(cfg["max_positions"]) == 5
    assert int(cfg["futures_entry_verify_calls_daily"]) == 10
    assert int(cfg["futures_entry_reserve_calls_daily"]) == 8

    t5 = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
    t14 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    n5 = session_priority_paced_call_cap(10, lane="normal", now=t5)
    r5 = session_priority_paced_call_cap(8, lane="reserve", now=t5)
    n14 = session_priority_paced_call_cap(10, lane="normal", now=t14)
    r14 = session_priority_paced_call_cap(8, lane="reserve", now=t14)
    assert n5["paced_max_calls"] == 3, n5
    assert r5["paced_max_calls"] == 2, r5
    assert n14["paced_max_calls"] == 8, n14
    assert r14["paced_max_calls"] == 6, r14
    assert n14["session_phase"] == "london_ny_overlap"

    hot = {"ticker_24h_pct": 10.0, "atr_pct": 0.72, "realized_vol_20_pct": 0.40}
    exceptional_n = session_opportunity_aware_paced_call_cap(10, lane="normal", now=t14, snapshot=hot, proposal_quality=0.967, proposal_setup=0.84)
    exceptional_r = session_opportunity_aware_paced_call_cap(8, lane="reserve", now=t14, snapshot=hot, proposal_quality=0.967, proposal_setup=0.84)
    assert exceptional_n["paced_max_calls"] == 10, exceptional_n
    assert exceptional_r["paced_max_calls"] == 8, exceptional_r
    assert exceptional_n["session_exceptional_burst_active"] is True
    assert exceptional_r["session_exceptional_burst_active"] is True
    # Same exceptional setup overnight cannot drain all day capacity.
    overnight = session_opportunity_aware_paced_call_cap(8, lane="reserve", now=t5, snapshot=hot, proposal_quality=0.967, proposal_setup=0.84)
    assert overnight["paced_max_calls"] == 3, overnight
    assert overnight["session_exceptional_burst_active"] is False

    # Ten consecutive ordinary losses no longer create an eight-hour no-trading timer.
    learning = live_learning_snapshot(FakeClosedPnlClient(loss=-0.5, count=10), 80.0)
    assert learning["loss_streak"] == 10, learning
    assert learning["pause"] is False, learning
    assert float(learning["effective_risk_pct"]) > 0.0, learning
    assert any("time-based trading shutdown disabled" in x for x in learning["notes"]), learning

    # True realized-loss hard stop remains authoritative.
    hard = live_learning_snapshot(FakeClosedPnlClient(loss=-3.0, count=10), 80.0)
    assert hard["pause"] is True, hard
    assert float(hard["effective_risk_pct"]) == 0.0, hard
    assert any("hard-stop reached" in x for x in hard["notes"]), hard

    first = ensure_budget_epoch("v4.6.8")
    carried = ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})
    assert carried["carried_forward"] is True and carried["baseline_rowid"] == first["baseline_rowid"], carried
    assert budget_epoch_status()["version"] == "v4.6.9"

    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    assert "session_opportunity_aware_paced_call_cap" in engine
    assert "session_exceptional_burst_active" in engine
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account
    print("v4.6.9 day-session continuity smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
