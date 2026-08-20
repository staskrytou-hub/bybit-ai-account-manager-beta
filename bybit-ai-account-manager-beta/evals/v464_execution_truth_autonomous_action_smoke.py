from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v464-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))

class FilledFlatClient:
    def get_order_realtime(self, **kwargs):
        return [{"orderStatus": "Filled", "orderId": "OID1", "orderLinkId": "LINK1", "cumExecQty": "2", "avgPrice": "100"}]

    def get_order_history(self, **kwargs):
        return []

    def get_executions(self, **kwargs):
        return [{"execQty": "2", "execPrice": "100", "execId": "E1"}]

    def get_positions(self, **kwargs):
        return [{"symbol": "TESTUSDT", "size": "0", "side": ""}]

    def set_trading_stop(self, **kwargs):
        raise AssertionError("flat fill must not try to set live-position stop")


class RejectedClient(FilledFlatClient):
    def get_order_realtime(self, **kwargs):
        return [{"orderStatus": "Rejected", "orderId": "OID2", "orderLinkId": "LINK2", "cumExecQty": "0"}]

    def get_executions(self, **kwargs):
        return []


class ProtectionFailClient(FilledFlatClient):
    def get_positions(self, **kwargs):
        return [{"symbol": "TESTUSDT", "size": "2", "side": "Buy", "stopLoss": "", "takeProfit": ""}]

    def set_trading_stop(self, **kwargs):
        raise RuntimeError("simulated protection failure")


def main() -> None:
    from trading_config import apply_safe_autopilot_profile
    from execution_verifier import confirm_market_entry
    from trading_store import get_state
    from trade_proposal import (
        record_proposal_approval, reusable_proposal_approval, clear_proposal_approval,
        proposal_stats,
    )
    from opportunity_manager import merge_official_events_into_plan

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) >= 15, cfg
    assert int(cfg["proposal_approval_minutes"]) == 45
    assert int(cfg["proposal_reverify_minutes"]) == 45
    assert int(cfg["browser_action_refresh_minutes"]) == 720
    assert float(cfg["growth_learning_exposure_pct"]) == 225.0
    # v4.6.4 intentionally increases capital utilization, not loss-at-stop catastrophe risk.
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert float(cfg["portfolio_absolute_risk_cap_usdt"]) == 20.0
    assert float(cfg["live_order_slippage_pct"]) == 0.25
    assert float(cfg["live_order_slippage_cap_pct"]) == 0.75

    proposal = {
        "eligible": True, "symbol": "TESTUSDT", "action": "long",
        "signature": "TESTUSDT:long:stable", "quality": 0.90,
    }
    record_proposal_approval("TESTUSDT", "15", signature=proposal["signature"], action="long", confidence=0.76, model="test", minutes=45, lane="futures")
    reused = reusable_proposal_approval(proposal, "15", "futures")
    assert reused and reused["action"] == "long", reused
    changed = dict(proposal, signature="TESTUSDT:long:changed")
    assert reusable_proposal_approval(changed, "15", "futures") == {}
    clear_proposal_approval("TESTUSDT", "15", "futures")

    fill = confirm_market_entry(FilledFlatClient(), symbol="TESTUSDT", order_id="OID1", order_link_id="LINK1", stop_loss=95, take_profit=110, timeout_seconds=3)
    assert fill["confirmed"] is True and fill["filled"] is True, fill
    assert fill["position_open"] is False, fill
    assert fill["lifecycle"] == "filled_flat_before_confirmation", fill
    rejected = confirm_market_entry(RejectedClient(), symbol="TESTUSDT", order_id="OID2", order_link_id="LINK2", stop_loss=95, take_profit=110, timeout_seconds=3)
    assert rejected["confirmed"] is False and rejected["filled"] is False, rejected
    assert rejected["lifecycle"] == "terminal_without_fill", rejected
    protection = confirm_market_entry(ProtectionFailClient(), symbol="TESTUSDT", order_id="OID3", order_link_id="LINK3", stop_loss=95, take_profit=110, timeout_seconds=3)
    assert protection["confirmed"] is True and protection["filled"] is True, protection
    assert protection["position_open"] is True and protection["protected"] is False, protection
    assert protection["lifecycle"] == "filled_open_protection_unverified", protection
    assert get_state("execution_safety_lock", "0") == "1"

    stats = proposal_stats("futures")
    for key in ("submitted", "confirmed", "execution_failed", "execution_uncertain", "ai_reused", "executed"):
        assert key in stats, stats

    # Multi-control deterministic zero-fund behavior is asserted from source below.

    plan = merge_official_events_into_plan(
        {"tracked": [], "human_action_required": [], "automatic_trade_alignment": []},
        [{"official_api": True, "bucket": "promotion", "title": "Reward Challenge", "description": "Join and claim", "url": "https://announcements.bybit.com/en-US/article/test", "symbols": []}],
    )
    assert plan["official_event_browser_queue_added"] == 1, plan
    assert plan["tracked"] and plan["tracked"][0]["actionability"] == "official_event_browser_discovery", plan

    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    spot = (ROOT / "spot_engine.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    promo = (ROOT / "promotion_executor.py").read_text(encoding="utf-8")
    ui = (ROOT / "account_os_ui.py").read_text(encoding="utf-8")
    assert "live Futures position already open on symbol" in engine
    assert 'reserve_ai_call(\n                    "futures_entry_verify"' in engine
    assert 'proposal_kind = "futures_entry_reserve"' in engine
    assert '"token reserve would exceed budget"' in engine
    assert "_adaptive_market_slippage_pct" in engine
    assert "reusable_proposal_approval" in engine and "approved_cached" in engine
    assert '"executed": filled' in engine or '"executed":filled' in engine.replace(" ", "")
    assert "filled = bool(confirmation.get(\"confirmed\")) and bool(confirmation.get(\"filled\"))" in engine
    assert 'bump_proposal_stat("submitted", lane="spot"' in spot
    assert 'bump_proposal_stat("executed", lane="spot"' in spot  # only monitor-side confirmed fill
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account
    assert "merge_official_events_into_plan" in account
    assert "explicit allowlisted zero-fund action selected deterministically" in promo
    assert 'actionability.startswith("not_actionable_")' in promo
    assert "CONFIRMED FILL" in ui

    print("v4.6.4 execution truth / autonomous action smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
