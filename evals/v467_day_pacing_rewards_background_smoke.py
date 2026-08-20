from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v467-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    from trading_config import apply_safe_autopilot_profile
    from trading_usage import legacy_paced_daily_call_cap, ensure_budget_epoch, ensure_budget_epoch_compatible, budget_epoch_status
    from execution_eligibility import STATE_KEY, mark_symbol_restriction, symbol_restriction, mark_family_restriction, family_restriction, instrument_restriction_family
    from trading_store import get_state, set_state
    from promotion_lifecycle import update_lifecycle, reward_audit_snapshot

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) == 20, cfg
    assert cfg["futures_entry_pacing_enabled"] is True
    assert int(cfg["futures_entry_pacing_window_hours"]) == 4
    assert int(cfg["futures_entry_verify_calls_daily"]) == 10
    assert int(cfg["futures_entry_reserve_calls_daily"]) == 8
    assert cfg["spot_entry_pacing_enabled"] is True
    assert int(cfg["spot_entry_pacing_window_hours"]) == 4
    assert int(cfg["browser_action_refresh_hours"]) == 12
    assert int(cfg["browser_action_refresh_minutes"]) == 720
    assert int(cfg["browser_action_max_cycles_daily"]) == 2
    assert cfg["browser_background_only"] is True
    # v4.6.7 must not silently change the profitable/risk core while fixing orchestration.
    assert float(cfg["growth_learning_risk_pct"]) == 4.0
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert float(cfg["portfolio_absolute_risk_cap_usdt"]) == 20.0

    # 18 daily entry calls are paced across the UTC day, not granted at midnight.
    t1 = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
    t21 = datetime(2026, 8, 18, 21, 0, tzinfo=timezone.utc)
    assert legacy_paced_daily_call_cap(10, lane="normal", now=t1)["paced_max_calls"] == 4
    assert legacy_paced_daily_call_cap(8, lane="reserve", now=t1)["paced_max_calls"] == 2
    assert legacy_paced_daily_call_cap(10, lane="normal", now=t10)["paced_max_calls"] == 7
    assert legacy_paced_daily_call_cap(8, lane="reserve", now=t10)["paced_max_calls"] == 4
    assert legacy_paced_daily_call_cap(10, lane="normal", now=t21)["paced_max_calls"] == 10
    assert legacy_paced_daily_call_cap(8, lane="reserve", now=t21)["paced_max_calls"] == 8

    # ETF TradFi contracts share the observed stock/ETF agreement family, so a new ETF
    # sibling (e.g. SNXX after SOXL failed) is filtered before another paid rediscovery.
    assert instrument_restriction_family({"symbolType": "ETF", "symbol": "SNXXUSDT"}) == "tradfi_stock_metal"

    # Agreement rejects persist even after the old 24h timestamp passes.
    mark_symbol_restriction("SNDKUSDT", "BybitAPIError: Bybit retCode=110126: You must sign the required agreement before trading this contract.")
    raw = json.loads(get_state(STATE_KEY, "{}") or "{}")
    raw["SNDKUSDT"]["blocked_until_ts"] = 1.0
    set_state(STATE_KEY, json.dumps(raw))
    direct = symbol_restriction("SNDKUSDT")
    assert direct["blocked"] is True and direct.get("persistent") is True, direct

    mark_family_restriction("tradfi_stock_metal", "BybitAPIError: Bybit retCode=110126: You must sign the required agreement before trading this contract.", source_symbol="SNDKUSDT")
    raw = json.loads(get_state(STATE_KEY, "{}") or "{}")
    raw["__family__:tradfi_stock_metal"]["blocked_until_ts"] = 1.0
    set_state(STATE_KEY, json.dumps(raw))
    fam = family_restriction("tradfi_stock_metal")
    assert fam["blocked"] is True and fam.get("persistent") is True, fam

    campaign = {
        "campaign_key": "audit-test",
        "name": "Audit Test Campaign",
        "source_url": "https://announcements.bybit.com/en/article/audit-test/",
        "requires_registration": True,
        "actionability": "official_event_browser_discovery",
        "trading_volume_requirement_usd": 5000.0,
    }
    update_lifecycle(campaign, "REGISTERED", evidence="registration control changed", action="Register", url=campaign["source_url"], verified=True)
    audit = reward_audit_snapshot({"tracked": [campaign]})
    assert audit["campaign_count"] == 1
    row = audit["items"][0]
    assert row["registration_status"] == "registered"
    assert row["trading_volume_progress_usd"] is None
    assert row["trading_volume_progress_status"] == "not_verified_from_campaign_account_state"
    assert row["natural_volume_only"] is True

    # Same-day hotfix never creates a second paid-AI allowance.
    first = ensure_budget_epoch("v4.6.6")
    carried = ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})
    assert carried["carried_forward"] is True and carried["baseline_rowid"] == first["baseline_rowid"], carried
    assert budget_epoch_status()["version"] == "v4.6.9"

    browser = (ROOT / "browser_operator.py").read_text(encoding="utf-8")
    promo = (ROOT / "promotion_executor.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    ui = (ROOT / "account_os_ui.py").read_text(encoding="utf-8")
    assert "page.bring_to_front()" not in browser
    assert '"headless": bool(self._background_only)' in browser
    assert "background reward cycle never launches a visible browser" in promo
    assert "browser_operator_cycle_history_v467" in account
    assert "browser_action_max_cycles_daily" in account
    assert "current_learning_state" in engine
    assert "ai_entry_pacing" in engine
    assert '"reward_audit": st.get("reward_audit", {})' in ui
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account

    print("v4.6.7 regression under v4.6.9: persistent eligibility / background rewards / legacy Spot pacing PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
