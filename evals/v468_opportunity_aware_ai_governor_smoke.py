from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v468-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    from trading_config import apply_safe_autopilot_profile
    from trading_usage import paced_daily_call_cap, opportunity_aware_paced_call_cap, ensure_budget_epoch, ensure_budget_epoch_compatible, budget_epoch_status

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) == 20, cfg
    assert int(cfg["futures_entry_verify_calls_daily"]) == 10
    assert int(cfg["futures_entry_reserve_calls_daily"]) == 8
    assert int(cfg["futures_ai_tokens_daily"]) == 50000
    assert cfg["futures_opportunity_governor_enabled"] is True
    assert abs(float(cfg["futures_ai_normal_min_quality"]) - 0.70) < 1e-9
    assert abs(float(cfg["futures_ai_reserve_min_quality"]) - 0.82) < 1e-9
    assert int(cfg["futures_opportunity_borrow_calls"]) == 1
    # Risk core is deliberately unchanged.
    assert float(cfg["growth_learning_risk_pct"]) == 4.0
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert float(cfg["portfolio_absolute_risk_cap_usdt"]) == 20.0
    # Reward browser nuisance controls are preserved.
    assert int(cfg["browser_action_max_cycles_daily"]) == 2
    assert cfg["browser_background_only"] is True

    t1 = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)
    t17 = datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc)
    # Smooth base pacing is monotone and leaves late-day capacity.
    n1 = paced_daily_call_cap(10, lane="normal", now=t1)
    n10 = paced_daily_call_cap(10, lane="normal", now=t10)
    n17 = paced_daily_call_cap(10, lane="normal", now=t17)
    r1 = paced_daily_call_cap(8, lane="reserve", now=t1)
    r10 = paced_daily_call_cap(8, lane="reserve", now=t10)
    r17 = paced_daily_call_cap(8, lane="reserve", now=t17)
    assert [n1["paced_max_calls"], n10["paced_max_calls"], n17["paced_max_calls"]] == [4, 7, 9]
    assert [r1["paced_max_calls"], r10["paced_max_calls"], r17["paced_max_calls"]] == [2, 5, 7]
    assert n17["daily_max_calls"] == 10 and r17["daily_max_calls"] == 8

    hot = {"ticker_24h_pct": 8.7, "atr_pct": 1.05, "realized_vol_20_pct": 0.8}
    borrowed_n = opportunity_aware_paced_call_cap(10, lane="normal", now=t17, snapshot=hot, proposal_quality=0.88, proposal_setup=0.75)
    borrowed_r = opportunity_aware_paced_call_cap(8, lane="reserve", now=t17, snapshot=hot, proposal_quality=0.88, proposal_setup=0.75)
    assert borrowed_n["base_paced_max_calls"] == 9 and borrowed_n["paced_max_calls"] == 10
    assert borrowed_r["base_paced_max_calls"] == 7 and borrowed_r["paced_max_calls"] == 8
    assert borrowed_n["opportunity_borrow_active"] is True
    assert borrowed_r["opportunity_borrow_active"] is True
    # A merely acceptable q=0.77 proposal cannot borrow future capacity.
    weak = opportunity_aware_paced_call_cap(8, lane="reserve", now=t17, snapshot=hot, proposal_quality=0.77, proposal_setup=0.75)
    assert weak["paced_max_calls"] == weak["base_paced_max_calls"] == 7
    assert weak["opportunity_borrow_active"] is False

    first = ensure_budget_epoch("v4.6.7")
    carried = ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})
    assert carried["carried_forward"] is True and carried["baseline_rowid"] == first["baseline_rowid"], carried
    assert budget_epoch_status()["version"] == "v4.6.9"

    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    assert "opportunity_aware_paced_call_cap" in engine
    assert "proposal q={proposal_quality:.2f} below paid-AI floor" in engine
    assert "reserve_candidate = high_priority and proposal_quality >= reserve_min_quality" in engine
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account

    print("v4.6.8 opportunity-aware AI governor smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
