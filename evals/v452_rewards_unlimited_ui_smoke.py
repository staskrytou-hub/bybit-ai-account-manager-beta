from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v452-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    import trading_usage as tu
    from trading_config import apply_safe_autopilot_profile, load_trading_settings, save_trading_settings
    from promotion_lifecycle import lifecycle_summary, update_lifecycle
    from browser_operator import BybitBrowserOperator

    # Old persisted budget values must not revive the old cooling mode when hard limits are disabled.
    save_trading_settings({
        "mode": "autopilot_live", "trading_token_budget_daily": 90000,
        "ai_max_calls_daily": 24, "ai_hard_limits_enabled": False,
    })
    cfg = load_trading_settings()
    assert cfg["trading_token_budget_daily"] == 0, cfg
    assert cfg["ai_max_calls_daily"] == 0, cfg
    assert cfg["ai_hard_limits_enabled"] is False, cfg
    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert cfg["trading_token_budget_daily"] == 0 and cfg["ai_max_calls_daily"] == 0, cfg
    assert cfg["browser_action_refresh_hours"] == 12 and cfg["browser_action_max_cycles_daily"] == 2 and cfg["browser_max_actions_per_cycle"] == 8, cfg

    # Unlimited AI still dedupes the exact same evidence but immediately admits changed evidence.
    old_db = tu.TRADING_DB
    tu.TRADING_DB = TEMP / "usage.db"
    try:
        ok, reason = tu.reserve_ai_call("futures_decision", budget=0, max_calls=0, cooldown_key="f:BTC:15", cooldown_seconds=2700, signature="state-a")
        assert ok, reason
        same, same_reason = tu.reserve_ai_call("futures_decision", budget=0, max_calls=0, cooldown_key="f:BTC:15", cooldown_seconds=2700, signature="state-a")
        assert not same and "same evidence" in same_reason.lower(), same_reason
        changed, changed_reason = tu.reserve_ai_call("futures_decision", budget=0, max_calls=0, cooldown_key="f:BTC:15", cooldown_seconds=2700, signature="state-b")
        assert changed, changed_reason
        status = tu.ai_budget_status(budget=0, max_calls=0)
        assert status["unlimited_tokens"] and status["unlimited_calls"] and not status["cooling"], status
    finally:
        tu.TRADING_DB = old_db

    # Promotion lifecycle progresses and does not regress after a verified claim.
    campaign = {"campaign_key":"eval-campaign", "name":"Eval Campaign", "source_url":"https://www.bybit.com/en/rewards_hub"}
    update_lifecycle(campaign, "DISCOVERED", evidence="found")
    update_lifecycle(campaign, "ELIGIBLE", evidence="eligible")
    update_lifecycle(campaign, "REGISTERED", evidence="registered", verified=True)
    update_lifecycle(campaign, "CLAIMED", evidence="claimed", verified=True)
    final = update_lifecycle(campaign, "DISCOVERED", evidence="rescan")
    assert final["state"] == "CLAIMED", final
    summary = lifecycle_summary([campaign])
    assert summary["counts"].get("CLAIMED") == 1, summary

    # Conservative post-click inference supports the requested reward lifecycle.
    before = {"safe_action_candidates":[{"text":"Register"}], "text":"Register now"}
    after = {"safe_action_candidates":[], "text":"You are registered and participating"}
    inferred = BybitBrowserOperator.infer_action_state(before, after, "Register")
    assert inferred["state"] == "REGISTERED" and inferred["verified"], inferred
    claim_before = {"safe_action_candidates":[{"text":"Claim"}], "text":"Claim reward"}
    claim_after = {"safe_action_candidates":[], "text":"Successfully claimed. Reward received."}
    claimed = BybitBrowserOperator.infer_action_state(claim_before, claim_after, "Claim")
    assert claimed["state"] == "CLAIMED" and claimed["verified"], claimed

    print("v4.5.2 unlimited AI + rewards lifecycle smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
