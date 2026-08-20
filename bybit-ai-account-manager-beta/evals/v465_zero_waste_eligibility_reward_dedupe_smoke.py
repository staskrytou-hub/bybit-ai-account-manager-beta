from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v465-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    from trading_config import apply_safe_autopilot_profile
    from trading_usage import (
        budget_epoch_status, budgeted_trading_tokens_today,
        ensure_budget_epoch, ensure_budget_epoch_compatible, record_trading_tokens,
    )
    from promotion_lifecycle import campaign_key, update_lifecycle, get_lifecycle, action_retry_allowed
    from promotion_executor import _decide, _match_known_campaign, _rewards_hub_campaign
    from opportunity_manager import merge_official_events_into_plan
    from execution_eligibility import (
        classify_exchange_restriction, execution_restrictions, family_restriction,
        instrument_restriction_family, mark_symbol_restriction, symbol_or_family_restriction,
        symbol_restriction,
    )

    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    assert int(cfg["autopilot_profile_version"]) >= 16, cfg
    assert int(cfg["promotion_action_tokens_daily"]) == 0, cfg
    assert int(cfg["promotion_action_calls_daily"]) == 0, cfg
    # v4.6.5 changes waste/eligibility, not the already-aggressive stop-risk envelope.
    assert float(cfg["absolute_risk_cap_pct"]) == 7.0
    assert float(cfg["portfolio_absolute_risk_cap_usdt"]) == 20.0

    # Hotfix install must not grant a fresh same-day paid-AI allowance.
    first = ensure_budget_epoch("v4.6.4")
    record_trading_tokens(1234, kind="futures_entry_verify")
    before = budgeted_trading_tokens_today("futures_entry_verify")
    carried = ensure_budget_epoch_compatible("v4.6.5", {"v4.6.4"})
    after = budgeted_trading_tokens_today("futures_entry_verify")
    assert before == 1234 and after == 1234, (before, after)
    assert carried["carried_forward"] is True, carried
    assert carried["baseline_rowid"] == first["baseline_rowid"], (carried, first)
    assert budget_epoch_status()["version"] == "v4.6.5"

    # Same official campaign in /en/ and /en-US/ plus query params => one canonical lifecycle.
    c1 = {
        "campaign_key": "global-example",
        "name": "Final Card Matching Challenge",
        "source_url": "https://announcements.bybit.com/en/article/the-final-card-matching-challenge-round-is-live-221-000-usdt-in-rewards-await--art587f78526499/?category=latest_activities",
    }
    c2 = {
        "campaign_key": "official-other-source-key",
        "name": "The final Card Matching Challenge round is live: 221,000 USDT in rewards await",
        "source_url": "https://announcements.bybit.com/en-US/article/the-final-card-matching-challenge-round-is-live-221-000-usdt-in-rewards-await--art587f78526499/",
    }
    assert campaign_key(c1) == campaign_key(c2), (campaign_key(c1), campaign_key(c2))
    update_lifecycle(c1, "REGISTERED", evidence="test", action="Register", url=c1["source_url"], verified=True)
    seen = get_lifecycle(c2)
    assert seen["state"] == "REGISTERED" and seen["verified"] is True, seen
    allowed, why = action_retry_allowed(c2, action_label="Register")
    assert allowed is False and "already" in why, (allowed, why)
    allowed_claim, _ = action_retry_allowed(c2, action_label="Claim")
    assert allowed_claim is True

    # Explicit safe DOM controls never invoke a paid model; ambiguous pages are tracked locally.
    safe = _decide(c2, {
        "login_required": False,
        "human_verification": False,
        "safe_action_candidates": [
            {"text": "Register", "context": "Card Matching Challenge"},
            {"text": "Claim", "context": "Card Matching Challenge"},
        ],
    })
    assert safe.action == "click" and safe.button_text == "Claim" and "no AI tokens" in safe.reason, safe
    ambiguous = _decide(c2, {"login_required": False, "human_verification": False, "safe_action_candidates": []})
    assert ambiguous.action == "track" and "skips paid AI" in ambiguous.reason, ambiguous

    # Strict local Rewards Hub matching reuses the official campaign's canonical lifecycle key.
    candidate = {
        "text": "Join Now",
        "context": "The final Card Matching Challenge round is live 221,000 USDT in rewards await. Join Now",
    }
    matched = _match_known_campaign(candidate, [c2])
    assert matched and campaign_key(matched) == campaign_key(c2), matched
    hub = _rewards_hub_campaign(candidate, [c2])
    assert hub["campaign_key"] == campaign_key(c2), hub

    # Opportunity OS must not enqueue the same article a second time via a locale variant.
    plan = merge_official_events_into_plan(
        {"tracked": [], "human_action_required": [c1], "automatic_trade_alignment": []},
        [{
            "official_api": True,
            "bucket": "promotion",
            "title": c2["name"],
            "description": "Reward challenge",
            "url": c2["source_url"],
            "symbols": [],
        }],
    )
    assert plan["official_event_browser_queue_added"] == 0, plan
    assert int(plan.get("official_event_duplicate_skipped", 0)) >= 1, plan

    # Exchange agreement/region/access failures are deterministic pre-AI restrictions.
    cls = classify_exchange_restriction("BybitAPIError: Bybit retCode=110126: You must sign the required agreement before trading this contract.")
    assert cls and cls["class"] == "agreement_required", cls
    marked = mark_symbol_restriction("SNDKUSDT", "BybitAPIError: Bybit retCode=110126: You must sign the required agreement before trading this contract.")
    assert marked and marked["blocked"] is True and marked["human_action_required"] is True, marked
    assert symbol_restriction("SNDKUSDT")["blocked"] is True
    assert instrument_restriction_family({"symbol": "SNDKUSDT", "symbolType": "stock"}) == "tradfi_stock_metal"
    # Seeing public instrument metadata promotes the known SNDK Trading-Terms reject into a
    # family-wide block, so sibling stock/commodity/forex contracts cost zero verifier tokens.
    sndk_block = symbol_or_family_restriction("SNDKUSDT", {"symbol": "SNDKUSDT", "symbolType": "stock"})
    assert sndk_block["blocked"] is True and sndk_block.get("matched_family") == "tradfi_stock_metal", sndk_block
    assert family_restriction("tradfi_stock_metal")["blocked"] is True
    soxl_block = symbol_or_family_restriction("SOXLUSDT", {"symbol": "SOXLUSDT", "symbolType": "stock"})
    xau_block = symbol_or_family_restriction("XAUUSDT", {"symbol": "XAUUSDT", "symbolType": "commodity"})
    oil_block = symbol_or_family_restriction("CLUSDT", {"symbol": "CLUSDT", "symbolType": "commodity"})
    forex_block = symbol_or_family_restriction("EURUSDT", {"symbol": "EURUSDT", "symbolType": "forex"})
    btc_block = symbol_or_family_restriction("BTCUSDT", {"symbol": "BTCUSDT", "symbolType": "innovation"})
    assert soxl_block["blocked"] is True and xau_block["blocked"] is True, (soxl_block, xau_block)
    assert oil_block["blocked"] is False and forex_block["blocked"] is False and btc_block["blocked"] is False, (oil_block, forex_block, btc_block)
    status = execution_restrictions()
    assert any(x.get("symbol") == "SNDKUSDT" for x in status["active"]), status

    engine = (ROOT / "trading_engine.py").read_text(encoding="utf-8")
    promo = (ROOT / "promotion_executor.py").read_text(encoding="utf-8")
    account = (ROOT / "account_os.py").read_text(encoding="utf-8")
    ui = (ROOT / "account_os_ui.py").read_text(encoding="utf-8")
    assert "exchange eligibility block" in engine
    assert "blocked/infeasible contracts do not consume the four proposal" in engine
    assert "mark_symbol_restriction(symbol, submit_error" in engine
    assert "symbol_or_family_restriction(candidate_symbol, candidate_instrument)" in engine
    assert "mark_family_restriction(" in engine
    assert "reserve_ai_call" not in promo
    assert "DOM-only policy skips paid AI" in promo
    assert 'ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})' in account
    assert "0 paid action-AI tokens" in ui

    print("v4.6.5 zero-waste eligibility / reward dedupe smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
