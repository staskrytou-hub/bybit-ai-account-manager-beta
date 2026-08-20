from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from account_os_store import get_state, record_event, set_state
from browser_operator import BybitBrowserOperator, browser_available
from promotion_lifecycle import (
    action_retry_allowed,
    campaign_key,
    get_lifecycle,
    lifecycle_summary,
    resolve_auth_required,
    recover_stale_action_running,
    update_lifecycle,
)
from runtime_control import RuntimeStoppedError, runtime_stop_requested
from trading_config import load_trading_settings


class BrowserDecision(BaseModel):
    action: Literal["click", "track", "human"]
    button_text: str = Field(default="", max_length=180)
    reason: str = Field(max_length=800)
    safe_zero_fund_movement: bool = False


INSTRUCTIONS = (
    "You are Stan Promotion Action Verifier. You receive one official Bybit promotion and a snapshot of the user's already-authenticated Bybit web page. "
    "Choose at most one ZERO-FUND-MOVEMENT control. Register/Join/Participate/Enroll may be clicked even when the campaign later requires normal trading or another separately-gated task; clicking registration itself must not move funds. "
    "Claim/Spin/Check-in may be clicked when explicitly visible. Never click Deposit, Transfer, Withdraw, Buy, Purchase, Subscribe, Stake, Earn, P2P, Referral, Card, Pay, Convert, Loan or Borrow controls. "
    "Never bypass CAPTCHA, 2FA, security verification or login. If login/verification is required choose human. If the visible control is ambiguous choose track. Return structured data only."
)

CONTROL_BLOCK_WORDS = {
    "deposit", "transfer", "withdraw", "buy", "purchase", "subscribe", "stake", "earn", "p2p",
    "refer", "referral", "card", "pay", "convert", "loan", "borrow",
}
SAFE_WORDS = ("register", "join", "claim", "spin", "check in", "check-in", "participate", "enroll")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _official_action_url(c: dict[str, Any]) -> tuple[bool, str]:
    url = str(c.get("source_url") or "").strip().lower()
    if not url:
        return False, "no official action URL"
    if not (
        url.startswith("https://www.bybit.com/") or url.startswith("https://bybit.com/") or
        url.startswith("https://www.bybit.eu/") or url.startswith("https://bybit.eu/") or
        url.startswith("https://announcements.bybit.com/") or url.startswith("https://announcements.bybit.eu/")
    ):
        return False, "action URL is outside the official Bybit allowlist"
    return True, ""


def _safe_candidate(candidate: dict[str, Any]) -> bool:
    label = str(candidate.get("text") or "").strip().lower()
    return bool(label and any(w in label for w in SAFE_WORDS) and not any(w in label for w in CONTROL_BLOCK_WORDS))


def _browser_infra_failure(text: str) -> bool:
    low = str(text or "").lower()
    return any(x in low for x in (
        "targetclosed", "target page, context or browser has been closed", "target.createtarget",
        "browser disconnected", "context is stale", "page has been closed", "browser has been closed",
        "could not acquire the dedicated bybit browser profile", "cdp connect failed",
    ))


def _decide(campaign: dict[str, Any], page: dict[str, Any]) -> BrowserDecision:
    if runtime_stop_requested():
        raise RuntimeStoppedError("Stan stopped before promotion decision")
    candidates = [x for x in list(page.get("safe_action_candidates") or []) if isinstance(x, dict) and _safe_candidate(x)]
    # v4.6.5 ZERO-WASTE browser lane: every control Stan is permitted to click is already
    # explicitly enumerated by the DOM allowlist. Therefore a paid LLM cannot safely unlock any
    # additional action. If an allowlisted control exists, choose deterministically; otherwise
    # track the page without spending a single promotion-action token.
    if candidates and not page.get("login_required") and not page.get("human_verification"):
        def _priority(item: dict[str, Any]) -> tuple[int, int]:
            label = str(item.get("text", "")).strip().lower()
            if "claim" in label:
                return (0, len(label))
            if "check in" in label or "check-in" in label:
                return (1, len(label))
            if any(x in label for x in ("register", "join", "participate", "enroll")):
                return (2, len(label))
            if "spin" in label:
                return (3, len(label))
            return (9, len(label))
        chosen = sorted(candidates, key=_priority)[0]
        label = str(chosen.get("text", "")).strip()
        return BrowserDecision(action="click", button_text=label, reason="explicit allowlisted zero-fund action selected deterministically; no AI tokens used", safe_zero_fund_movement=True)
    if page.get("login_required") or page.get("human_verification"):
        return BrowserDecision(action="human", reason="visible login/2FA/CAPTCHA/security verification requires human completion", safe_zero_fund_movement=False)
    return BrowserDecision(action="track", reason="no explicit allowlisted zero-fund control is visible; DOM-only policy skips paid AI", safe_zero_fund_movement=False)


def _candidate_by_label(page: dict[str, Any], label: str) -> dict[str, Any] | None:
    target = str(label or "").casefold()
    for candidate in list(page.get("safe_action_candidates") or []):
        if isinstance(candidate, dict) and str(candidate.get("text", "")).casefold() == target and _safe_candidate(candidate):
            return candidate
    return None


def _record_transition(
    campaign: dict[str, Any], state: str, *, evidence: str, action: str = "", url: str = "",
    verified: bool = False, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = update_lifecycle(campaign, state, evidence=evidence, action=action, url=url, verified=verified, extra=extra)
    record_event(
        "promotion.lifecycle", f"{item.get('state', state)}: {campaign.get('name')}",
        {"state": item.get("state", state), "verified": item.get("verified", verified), "action": action, "evidence": evidence[:700], "url": url},
    )
    return item


def _mark_retry(campaign: dict[str, Any], reason: str, *, url: str = "", action: str = "") -> dict[str, Any]:
    prior = get_lifecycle(campaign)
    retries = int(prior.get("retry_count", 0) or 0) + 1
    return _record_transition(
        campaign, "RETRY_WAIT", evidence=reason, action=action, url=url, verified=False,
        extra={"retry_count": retries, "last_attempt_at": _now(), "last_error": str(reason)[:1200]},
    )


def _verify_pending_without_click(campaign: dict[str, Any], page: dict[str, Any]) -> dict[str, Any] | None:
    """Re-inspect a previously sent action without ever clicking it a second time."""
    lifecycle = get_lifecycle(campaign)
    state = str(lifecycle.get("state") or "").upper()
    if state != "ACTION_SENT_UNVERIFIED":
        return None
    action = str(lifecycle.get("last_action") or "").strip()
    if not action:
        return {"campaign": campaign.get("name"), "status": "waiting", "reason": "previous action was sent but its control label is unavailable; no duplicate click attempted", "lifecycle_state": state}
    before = {"safe_action_candidates": [{"text": action}]}
    inferred = BybitBrowserOperator.infer_action_state(before, page, action)
    verified = bool(inferred.get("verified"))
    if verified:
        next_state = str(inferred.get("state") or "VERIFIED_COMPLETE")
        evidence = str(inferred.get("evidence") or "pending action verified by re-inspection")
        _record_transition(campaign, next_state, evidence=evidence, action=action, url=str(page.get("url") or lifecycle.get("url") or ""), verified=True, extra={"retry_count": 0, "verified_after_reinspect_at": _now()})
        return {"campaign": campaign.get("name"), "status": next_state.lower(), "verified": True, "button": action, "evidence": evidence, "verification_only": True}
    return {"campaign": campaign.get("name"), "status": "waiting", "reason": "previous action remains unverified; re-inspected without duplicate click", "lifecycle_state": state, "verification_only": True}


def _process_campaign(browser: BybitBrowserOperator, campaign: dict[str, Any]) -> dict[str, Any]:
    if runtime_stop_requested():
        return {"campaign": campaign.get("name"), "status": "stopped", "reason": "runtime/manual STOP active"}
    ok, why = _official_action_url(campaign)
    if not ok:
        _record_transition(campaign, "DISCOVERED", evidence=why, url=str(campaign.get("source_url") or ""))
        return {"campaign": campaign.get("name"), "status": "track", "reason": why}

    lifecycle = _record_transition(campaign, "DISCOVERED", evidence="official Bybit campaign discovered", url=str(campaign.get("source_url") or ""))
    lifecycle = recover_stale_action_running(campaign)
    state = str(lifecycle.get("state") or "DISCOVERED").upper()
    if state == "CLAIMED" and bool(lifecycle.get("verified")):
        return {"campaign": campaign.get("name"), "status": state.lower(), "reason": "verified reward already claimed"}

    allowed, retry_reason = action_retry_allowed(campaign, cooldown_seconds=6 * 3600)
    if not allowed and state != "AUTH_REQUIRED":
        return {"campaign": campaign.get("name"), "status": "waiting", "reason": retry_reason, "lifecycle_state": state}

    try:
        page = browser.inspect(str(campaign.get("source_url")))
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _mark_retry(campaign, reason, url=str(campaign.get("source_url") or ""))
        return {"campaign": campaign.get("name"), "status": "retry_wait", "reason": reason}

    if page.get("login_required") or page.get("human_verification"):
        _record_transition(campaign, "AUTH_REQUIRED", evidence="Bybit login/2FA/CAPTCHA required; background reward cycle will not open or focus a visible browser", url=str(page.get("url") or ""))
        return {
            "campaign": campaign.get("name"), "status": "human",
            "reason": "Bybit authentication is required. Automated reward checks stay background-only; use Authorize Bybit Browser explicitly when convenient.",
            "url": page.get("url"),
        }

    resolve_auth_required(campaign, url=str(page.get("url") or ""))
    _record_transition(campaign, "ELIGIBLE", evidence="authenticated official campaign page is accessible", url=str(page.get("url") or ""))
    pending = _verify_pending_without_click(campaign, page)
    if pending is not None:
        return pending
    allowed, retry_reason = action_retry_allowed(campaign, cooldown_seconds=6 * 3600)
    if not allowed:
        return {"campaign": campaign.get("name"), "status": "waiting", "reason": retry_reason, "url": page.get("url")}

    try:
        decision = _decide(campaign, page)
    except RuntimeStoppedError:
        return {"campaign": campaign.get("name"), "status": "stopped", "reason": "runtime/manual STOP active"}
    except Exception as exc:
        # An AI verifier/runtime failure is not a browser-health failure. Keep the
        # authenticated Playwright session alive and retry this campaign later.
        reason = f"Promotion verifier unavailable: {type(exc).__name__}: {exc}"
        _mark_retry(campaign, reason, url=str(page.get("url") or campaign.get("source_url") or ""))
        return {"campaign": campaign.get("name"), "status": "retry_wait", "reason": reason, "url": page.get("url")}
    if decision.action != "click" or not decision.safe_zero_fund_movement or not decision.button_text:
        return {"campaign": campaign.get("name"), "status": decision.action, "reason": decision.reason, "url": page.get("url")}
    action_allowed, action_reason = action_retry_allowed(campaign, cooldown_seconds=6 * 3600, action_label=decision.button_text)
    if not action_allowed:
        return {"campaign": campaign.get("name"), "status": "waiting", "reason": action_reason, "url": page.get("url"), "button": decision.button_text}
    candidate = _candidate_by_label(page, decision.button_text)
    if not candidate:
        return {"campaign": campaign.get("name"), "status": "track", "reason": "selected safe control is no longer visible", "url": page.get("url")}
    if runtime_stop_requested():
        return {"campaign": campaign.get("name"), "status": "stopped", "reason": "runtime/manual STOP active"}

    _record_transition(
        campaign, "ACTION_RUNNING", evidence="safe zero-fund browser action starting", action=decision.button_text,
        url=str(page.get("url") or ""), extra={"last_attempt_at": _now()},
    )
    clicked = browser.click_safe_candidate(candidate)
    if not clicked.get("clicked"):
        reason = str(clicked.get("reason") or "safe control was not clickable")
        if _browser_infra_failure(reason):
            _mark_retry(campaign, reason, url=str(clicked.get("url") or page.get("url") or ""), action=decision.button_text)
            return {"campaign": campaign.get("name"), "status": "retry_wait", "reason": reason, "url": clicked.get("url")}
        _record_transition(campaign, "ELIGIBLE", evidence=reason, action=decision.button_text, url=str(clicked.get("url") or page.get("url") or ""))
        return {"campaign": campaign.get("name"), "status": "track", "reason": reason, "url": clicked.get("url")}

    if runtime_stop_requested():
        _record_transition(campaign, "ACTION_SENT_UNVERIFIED", evidence="click sent before STOP; verification deferred", action=decision.button_text, url=str(clicked.get("url") or ""), extra={"last_attempt_at": _now()})
        return {"campaign": campaign.get("name"), "status": "action_sent_unverified", "reason": "STOP pressed before verification"}

    try:
        verify = browser.inspect(str(clicked.get("url") or campaign.get("source_url")))
    except Exception as exc:
        reason = f"click was sent; verification deferred after browser error: {type(exc).__name__}: {exc}"
        _record_transition(campaign, "ACTION_SENT_UNVERIFIED", evidence=reason, action=decision.button_text, url=str(clicked.get("url") or ""), extra={"last_attempt_at": _now(), "last_error": reason[:1200]})
        return {"campaign": campaign.get("name"), "status": "action_sent_unverified", "reason": reason}

    inferred = browser.infer_action_state(page, verify, decision.button_text)
    state = str(inferred.get("state") or "ACTION_SENT_UNVERIFIED")
    verified = bool(inferred.get("verified"))
    evidence = str(inferred.get("evidence") or "post-action page inspected")
    _record_transition(
        campaign, state, evidence=evidence, action=decision.button_text, url=str(verify.get("url") or ""), verified=verified,
        extra={"last_attempt_at": _now(), "retry_count": 0 if verified else int(get_lifecycle(campaign).get("retry_count", 0) or 0)},
    )
    return {
        "campaign": campaign.get("name"), "status": state.lower(), "verified": verified,
        "button": decision.button_text, "reason": decision.reason, "evidence": evidence,
        "url": verify.get("url"), "at": _now(),
    }


def _norm_campaign_text(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    stop = {"bybit", "reward", "rewards", "trade", "trading", "usdt", "join", "register", "claim", "now", "the", "and", "your", "with", "for", "from"}
    return " ".join(x for x in text.split() if len(x) >= 3 and x not in stop)


def _match_known_campaign(candidate: dict[str, Any], known_campaigns: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Strict local fuzzy bridge between Rewards Hub cards and official announcement rows.

    We only merge when the card context contains the normalized campaign title or at least three
    distinctive title tokens with strong overlap. This prevents a Rewards Hub Join button from
    creating a second lifecycle for the same official event without relying on AI.
    """
    context = _norm_campaign_text(str(candidate.get("context") or "") + " " + str(candidate.get("text") or ""))
    if not context:
        return None
    context_tokens = set(context.split())
    best: tuple[float, dict[str, Any]] | None = None
    for row in list(known_campaigns or []):
        if not isinstance(row, dict):
            continue
        name = _norm_campaign_text(str(row.get("name") or ""))
        if not name:
            continue
        name_tokens = set(name.split())
        if not name_tokens:
            continue
        contained = name in context or context in name
        common = name_tokens & context_tokens
        overlap = len(common) / max(1, len(name_tokens))
        score = 1.0 if contained else overlap
        if (contained and len(name_tokens) >= 2) or (len(common) >= 3 and overlap >= 0.60):
            if best is None or score > best[0]:
                best = (score, row)
    return dict(best[1]) if best else None


def _rewards_hub_campaign(candidate: dict[str, Any], known_campaigns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    context = " ".join(str(candidate.get("context") or "").split())[:1200]
    label = " ".join(str(candidate.get("text") or "").split())[:180]
    matched = _match_known_campaign(candidate, known_campaigns)
    key_material = context.lower() or label.lower()
    if "check" in label.lower():
        key_material += "|" + datetime.now(timezone.utc).date().isoformat()
    key = campaign_key(matched) if matched else "rewards-" + hashlib.sha256(key_material.encode("utf-8", errors="ignore")).hexdigest()[:28]
    short_context = context[:180] or "Account-specific Rewards Hub task"
    return {
        "campaign_key": key,
        "name": str((matched or {}).get("name") or f"Rewards Hub • {short_context}"),
        "source_url": "https://www.bybit.com/en/rewards_hub",
        "canonical_source_url": str((matched or {}).get("source_url") or ""),
        "matched_actionability": str((matched or {}).get("actionability") or ""),
        "requires_registration": any(x in label.lower() for x in ("register", "join", "participate", "enroll")),
        "account_specific": True,
        "tasks": [context] if context else [],
        "actionability": "browser_safe_action",
    }


def _process_rewards_hub(browser: BybitBrowserOperator, max_actions: int, *, deadline_monotonic: float | None = None, known_campaigns: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if runtime_stop_requested():
        return actions
    page = browser.inspect_rewards_hub()
    pseudo = {"campaign_key": "rewards-hub-auth", "name": "Bybit Rewards Hub", "source_url": page.get("url") or "https://www.bybit.com/en/rewards_hub"}
    if page.get("login_required") or page.get("human_verification"):
        _record_transition(pseudo, "AUTH_REQUIRED", evidence="Rewards Hub requires login/2FA/CAPTCHA; background cycle will not open/focus a visible browser", url=str(page.get("url") or ""))
        return [{
            "campaign": "Bybit Rewards Hub", "status": "human",
            "reason": "Authentication required; background reward cycle never launches a visible browser. Use Authorize Bybit Browser explicitly.",
        }]
    resolve_auth_required(pseudo, url=str(page.get("url") or ""), evidence="Rewards Hub authenticated session is accessible")

    # Verify pending Rewards Hub clicks from prior cycles before considering any new click.
    # This lets a disappeared Join/Claim control become REGISTERED/CLAIMED without waiting
    # six hours and, critically, without sending the action twice.
    for life in list((lifecycle_summary([]).get("items") or []))[:80]:
        if not isinstance(life, dict) or str(life.get("state") or "").upper() != "ACTION_SENT_UNVERIFIED":
            continue
        if "rewards_hub" not in str(life.get("url") or "").lower():
            continue
        pending_campaign = {
            "campaign_key": str(life.get("campaign_key") or ""),
            "name": str(life.get("campaign") or "Rewards Hub pending action"),
            "source_url": str(life.get("url") or page.get("url") or "https://www.bybit.com/en/rewards_hub"),
        }
        checked = _verify_pending_without_click(pending_campaign, page)
        if checked and checked.get("verified"):
            actions.append(checked)

    candidates = [x for x in list(page.get("safe_action_candidates") or []) if isinstance(x, dict) and _safe_candidate(x)]
    for candidate in candidates[:max_actions]:
        if runtime_stop_requested():
            break
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            actions.append({"campaign": "Bybit Rewards Hub", "status": "waiting", "reason": "browser cycle time budget reached; remaining safe actions deferred"})
            break
        campaign = _rewards_hub_campaign(candidate, known_campaigns)
        label = str(candidate.get("text") or "")
        matched_actionability = str(campaign.get("matched_actionability") or "").lower()
        if matched_actionability.startswith("not_actionable") or matched_actionability.startswith("ineligible"):
            actions.append({"campaign": campaign.get("name"), "status": "track", "reason": f"known campaign is {matched_actionability}; no registration click attempted"})
            continue
        lifecycle = update_lifecycle(campaign, "DISCOVERED", evidence="account-specific safe action visible in Rewards Hub", url=str(page.get("url") or ""))
        lifecycle = recover_stale_action_running(campaign)
        lifecycle_state = str(lifecycle.get("state") or "DISCOVERED").upper()
        if lifecycle_state in {"CLAIMED", "COMPLETED", "VERIFIED_COMPLETE"} and bool(lifecycle.get("verified")):
            continue
        if lifecycle_state == "REGISTERED" and bool(lifecycle.get("verified")) and any(x in label.lower() for x in ("register", "join", "participate", "enroll")):
            continue
        allowed, why = action_retry_allowed(campaign, cooldown_seconds=6 * 3600, action_label=label)
        if not allowed:
            actions.append({"campaign": campaign.get("name"), "status": "waiting", "reason": why})
            continue

        before = page
        _record_transition(campaign, "ACTION_RUNNING", evidence="safe Rewards Hub action starting", action=label, url=str(page.get("url") or ""), extra={"last_attempt_at": _now()})
        clicked = browser.click_safe_candidate(candidate)
        if not clicked.get("clicked"):
            reason = str(clicked.get("reason") or "safe control was not clickable")
            if _browser_infra_failure(reason):
                _mark_retry(campaign, reason, url=str(clicked.get("url") or ""), action=label)
                actions.append({"campaign": campaign.get("name"), "status": "retry_wait", "reason": reason})
            else:
                # ACTION_RUNNING was entered before the click. If the control changed
                # or could not be clicked, unwind into a cooldown state rather than
                # leaving a permanent ACTION_RUNNING record.
                _mark_retry(campaign, reason, url=str(clicked.get("url") or page.get("url") or ""), action=label)
                actions.append({"campaign": campaign.get("name"), "status": "retry_wait", "reason": reason})
            continue
        if runtime_stop_requested():
            _record_transition(campaign, "ACTION_SENT_UNVERIFIED", evidence="click sent before STOP; verification deferred", action=label, url=str(clicked.get("url") or ""), extra={"last_attempt_at": _now()})
            actions.append({"campaign": campaign.get("name"), "status": "action_sent_unverified", "reason": "STOP pressed before verification"})
            break
        try:
            page = browser.inspect_rewards_hub()
        except Exception as exc:
            reason = f"click was sent; verification deferred: {type(exc).__name__}: {exc}"
            _record_transition(campaign, "ACTION_SENT_UNVERIFIED", evidence=reason, action=label, url=str(clicked.get("url") or ""), extra={"last_attempt_at": _now(), "last_error": reason[:1200]})
            actions.append({"campaign": campaign.get("name"), "status": "action_sent_unverified", "reason": reason})
            continue
        inferred = browser.infer_action_state(before, page, label)
        state = str(inferred.get("state") or "ACTION_SENT_UNVERIFIED")
        verified = bool(inferred.get("verified"))
        evidence = str(inferred.get("evidence") or "Rewards Hub re-inspected after click")
        _record_transition(campaign, state, evidence=evidence, action=label, url=str(page.get("url") or ""), verified=verified, extra={"last_attempt_at": _now()})
        item = {"campaign": campaign.get("name"), "status": state.lower(), "verified": verified, "button": label, "evidence": evidence, "source": "rewards_hub"}
        set_state("promo_action:" + campaign_key(campaign), {**item, "lifecycle_state": state, "at": _now()})
        actions.append(item)
    return actions


def execute_safe_promotion_actions(plan: dict[str, Any]) -> dict[str, Any]:
    cycle_started_at = _now()
    if runtime_stop_requested():
        return {"available": True, "stopped": True, "actions": [], "reason": "runtime/manual STOP active", "state": "STOPPED"}
    availability = browser_available()
    if not availability.get("available"):
        result = {"available": False, "reason": availability.get("reason"), "actions": [], "state": availability.get("state", "UNAVAILABLE")}
        set_state("browser_operator_status", result)
        return result
    cfg = load_trading_settings()
    cycle_timeout_seconds = int(cfg.get("browser_cycle_timeout_seconds", 420) or 420)
    deadline_monotonic = time.monotonic() + max(120, cycle_timeout_seconds)
    if not bool(cfg.get("browser_operator_enabled", True)):
        result = {"available": True, "disabled": True, "actions": [], "state": "DISABLED"}
        set_state("browser_operator_status", result)
        return result

    max_actions = int(cfg.get("browser_max_actions_per_cycle", 8))
    # v4.6.4: spend Browser Operator time on fresh actionable/unknown official events before
    # already-known region-restricted campaigns. Rewards Hub is still inspected separately first.
    rows = list(plan.get("tracked") or []) + list(plan.get("automatic_trade_alignment") or []) + list(plan.get("human_action_required") or [])
    # Stable campaign-key dedupe within this cycle. Explicitly non-actionable campaigns are kept
    # in Opportunity OS for visibility but do not consume browser visits/actions.
    unique_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        actionability = str(row.get("actionability") or "").lower()
        if actionability.startswith("not_actionable_"):
            continue
        key = campaign_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    actions: list[dict[str, Any]] = []
    browser_health: dict[str, Any] = {}
    try:
        with BybitBrowserOperator(background_only=bool(cfg.get("browser_background_only", True))) as browser:
            browser_health = browser.health()
            try:
                hub_actions = _process_rewards_hub(browser, max_actions=max(1, max_actions // 2), deadline_monotonic=deadline_monotonic, known_campaigns=rows)
                actions.extend(hub_actions)
            except RuntimeStoppedError:
                actions.append({"status": "stopped", "reason": "runtime/manual STOP active"})
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                actions.append({"campaign": "Bybit Rewards Hub", "status": "retry_wait" if _browser_infra_failure(reason) else "error", "reason": reason})

            attempted = len([x for x in actions if x.get("status") not in {"track", "waiting", "error", "retry_wait"}])
            remaining = max(0, max_actions - attempted)
            for campaign in unique_rows:
                if remaining <= 0 or runtime_stop_requested():
                    break
                if time.monotonic() >= deadline_monotonic:
                    actions.append({"campaign": str(campaign.get("name") or "Bybit campaign"), "status": "waiting", "reason": "browser cycle time budget reached; remaining safe actions deferred"})
                    break
                item = _process_campaign(browser, campaign)
                actions.append(item)
                if item.get("status") not in {"track", "waiting", "error", "retry_wait", "human", "stopped"}:
                    remaining -= 1
                set_state("promo_action:" + campaign_key(campaign), {**item, "lifecycle_state": str(get_lifecycle(campaign).get("state", "")), "at": _now()})
                record_event("promotion.browser_action", f"{item.get('status')}: {campaign.get('name')}", item)
            browser_health = browser.health()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        actions.append({"campaign": "Bybit Browser Operator", "status": "retry_wait" if _browser_infra_failure(reason) else "error", "reason": reason})
        browser_health = {"state": "STALE" if _browser_infra_failure(reason) else "ERROR", "last_error": reason}

    summary = lifecycle_summary(unique_rows)
    current_errors = [x for x in actions if isinstance(x, dict) and str(x.get("status") or "").lower() in {"error", "retry_wait"}]
    finished_at = _now()
    resolved_state = "STOPPED" if runtime_stop_requested() else str(browser_health.get("state") or availability.get("state") or "AVAILABLE")
    result = {
        "available": True,
        "browser": availability.get("browser", ""),
        "browser_path": availability.get("path", ""),
        "state": resolved_state,
        "session_connected": bool(availability.get("session_connected")) or browser_health.get("connection_mode") == "cdp",
        "browser_health": browser_health,
        "actions": actions,
        "lifecycle": summary,
        "cycle_started_at": cycle_started_at,
        "cycle_finished_at": finished_at,
        "cycle_timeout_seconds": cycle_timeout_seconds,
        "cycle_time_budget_exhausted": any("cycle time budget reached" in str((x or {}).get("reason") or "") for x in actions if isinstance(x, dict)),
        "current_error_count": len(current_errors),
        "last_error": str(current_errors[-1].get("reason") or "")[:1200] if current_errors else "",
        "last_error_at": finished_at if current_errors else "",
        "last_success_at": finished_at if not current_errors and resolved_state not in {"ERROR", "STALE"} else "",
        "updated_at": finished_at,
    }
    set_state("browser_operator_status", result)
    set_state("promotion_lifecycle_summary", summary)
    return result
