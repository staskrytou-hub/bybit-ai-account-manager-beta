from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Any

from account_os_store import get_state, set_state

# v4.5.6 lifecycle states. Legacy states remain readable for upgrade compatibility.
LIFECYCLE_ORDER = {
    "DISCOVERED": 10,
    "AUTH_REQUIRED": 15,
    "ELIGIBLE": 20,
    "QUEUED": 25,
    "ACTION_RUNNING": 30,
    "ACTION_SENT_UNVERIFIED": 35,
    "REGISTERED": 40,
    "IN_PROGRESS": 35,  # legacy alias from <=v4.5.4
    "RETRY_WAIT": 36,
    "FAILED": 37,
    "VERIFIED_COMPLETE": 50,
    "COMPLETED": 50,
    "CLAIMED": 60,
    "EXPIRED": 70,
}
INDEX_KEY = "promo_lifecycle_index"
TERMINAL_VERIFIED = {"VERIFIED_COMPLETE", "COMPLETED", "CLAIMED"}
TRANSIENT_RECOVERABLE = {"AUTH_REQUIRED", "RETRY_WAIT", "FAILED", "ACTION_SENT_UNVERIFIED", "IN_PROGRESS"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_url_key(url: str) -> str:
    """Cross-source Bybit campaign identity independent of locale/query/tracking parameters."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/").lower()
    except Exception:
        return ""
    if host in {"announcements.bybit.com", "announcements.bybit.eu"}:
        # /en/article/<slug> and /en-US/article/<slug> are the same campaign.
        match = re.search(r"/article/([^/]+)$", path)
        if match:
            slug = match.group(1)
            return "bybit-article-" + hashlib.sha256(slug.encode("utf-8", errors="ignore")).hexdigest()[:32]
    return ""


def _legacy_campaign_key(campaign: dict[str, Any]) -> str:
    explicit = str(campaign.get("campaign_key") or "").strip()
    if explicit:
        return explicit[:180]
    material = "|".join([
        str(campaign.get("name") or ""),
        str(campaign.get("source_url") or ""),
        str(campaign.get("ends_at") or ""),
    ])
    return "auto-" + hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()[:32]


def campaign_key(campaign: dict[str, Any]) -> str:
    # v4.6.5: canonical official article identity wins over source-specific keys. This makes
    # Promotion Intelligence, Opportunity OS and Browser Operator share one durable lifecycle.
    canonical = _canonical_url_key(str(campaign.get("source_url") or ""))
    if canonical:
        return canonical[:180]
    return _legacy_campaign_key(campaign)


def _state_key(campaign: dict[str, Any]) -> str:
    return "promo_lifecycle:" + campaign_key(campaign)


def get_lifecycle(campaign: dict[str, Any]) -> dict[str, Any]:
    canonical_key = campaign_key(campaign)
    saved = get_state("promo_lifecycle:" + canonical_key, {})
    if not isinstance(saved, dict):
        saved = {}

    # Upgrade compatibility: v4.6.4 and older stored source-specific keys. If the same Bybit
    # announcement appears through another source, migrate the strongest matching URL record
    # into the canonical key instead of starting a duplicate registration lifecycle.
    if not saved:
        legacy = _legacy_campaign_key(campaign)
        if legacy != canonical_key:
            prior = get_state("promo_lifecycle:" + legacy, {})
            if isinstance(prior, dict) and prior:
                saved = dict(prior)
        if not saved and canonical_key.startswith("bybit-article-"):
            index = get_state(INDEX_KEY, [])
            if isinstance(index, list):
                for old_key in index[:200]:
                    prior = get_state("promo_lifecycle:" + str(old_key), {})
                    if not isinstance(prior, dict) or not prior:
                        continue
                    if _canonical_url_key(str(prior.get("url") or "")) == canonical_key:
                        saved = dict(prior)
                        break
        if saved:
            saved["campaign_key"] = canonical_key
            set_state("promo_lifecycle:" + canonical_key, saved)
            _index_key(canonical_key)

    if not saved:
        saved = {
            "campaign_key": canonical_key,
            "campaign": str(campaign.get("name") or "Unnamed campaign"),
            "state": "DISCOVERED",
            "updated_at": _now(),
            "history": [],
            "verified": False,
        }
    # Upgrade the ambiguous old IN_PROGRESS state without losing history.
    if str(saved.get("state") or "").upper() == "IN_PROGRESS":
        saved = dict(saved)
        saved["state"] = "ACTION_SENT_UNVERIFIED"
    return saved


def _index_key(key: str) -> None:
    index = get_state(INDEX_KEY, [])
    if not isinstance(index, list):
        index = []
    key = str(key)[:180]
    index = [x for x in index if str(x) != key]
    index.insert(0, key)
    set_state(INDEX_KEY, index[:200])


def _allow_transition(previous: str, requested: str, current_verified: bool) -> str:
    previous = "ACTION_SENT_UNVERIFIED" if previous == "IN_PROGRESS" else previous
    requested = "ACTION_SENT_UNVERIFIED" if requested == "IN_PROGRESS" else requested
    if previous in TERMINAL_VERIFIED and current_verified:
        # A verified success is durable. CLAIMED may advance a prior registered/completed item.
        if requested == "CLAIMED" and previous != "CLAIMED":
            return requested
        return previous
    # Rediscovery/inspection must not erase a pending click or retry cooldown. Authentication
    # is the one transient state that is explicitly cleared by successful login verification.
    if previous == "AUTH_REQUIRED" and requested in {"ELIGIBLE", "QUEUED", "ACTION_RUNNING"}:
        return requested
    if previous in {"ACTION_RUNNING", "ACTION_SENT_UNVERIFIED", "RETRY_WAIT"} and requested in {"DISCOVERED", "ELIGIBLE", "QUEUED"}:
        return previous
    if previous in {"RETRY_WAIT", "ACTION_SENT_UNVERIFIED", "FAILED"} and requested == "ACTION_RUNNING":
        return requested
    # Do not downgrade a positive verified registration just because the public campaign is rediscovered.
    if previous == "REGISTERED" and requested in {"DISCOVERED", "AUTH_REQUIRED", "ELIGIBLE", "QUEUED"}:
        return previous
    return requested


def update_lifecycle(
    campaign: dict[str, Any],
    state: str,
    *,
    evidence: str = "",
    action: str = "",
    url: str = "",
    verified: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested = str(state or "DISCOVERED").upper()
    if requested not in LIFECYCLE_ORDER:
        requested = "DISCOVERED"
    current = get_lifecycle(campaign)
    previous = str(current.get("state") or "DISCOVERED").upper()
    state = _allow_transition(previous, requested, bool(current.get("verified")))

    event = {
        "at": _now(),
        "state": state,
        "evidence": str(evidence or "")[:1600],
        "action": str(action or "")[:180],
        "url": str(url or "")[:1000],
        "verified": bool(verified),
    }
    history = list(current.get("history") or [])
    history.append(event)
    durable_verified = bool(verified) or (bool(current.get("verified")) and state == previous)
    # If rediscovery/inspection is intentionally prevented from downgrading a pending
    # action, do not erase the original action label with an empty DISCOVERED/ELIGIBLE event.
    durable_action = event["action"]
    if not durable_action and state == previous and previous in {"ACTION_RUNNING", "ACTION_SENT_UNVERIFIED", "RETRY_WAIT"}:
        durable_action = str(current.get("last_action") or "")[:180]
    current.update({
        "campaign_key": campaign_key(campaign),
        "campaign": str(campaign.get("name") or current.get("campaign") or "Unnamed campaign")[:300],
        "state": state,
        "verified": durable_verified,
        "updated_at": event["at"],
        "last_evidence": event["evidence"],
        "last_action": durable_action,
        "url": event["url"] or str(current.get("url") or ""),
        "history": history[-40:],
    })
    if extra:
        current.update(extra)
    set_state(_state_key(campaign), current)
    _index_key(current["campaign_key"])
    return current


def resolve_auth_required(campaign: dict[str, Any], *, url: str = "", evidence: str = "authenticated Bybit session verified") -> dict[str, Any]:
    """Clear a stale AUTH_REQUIRED lifecycle after real browser authentication succeeds."""
    current = get_lifecycle(campaign)
    if str(current.get("state") or "").upper() == "AUTH_REQUIRED":
        return update_lifecycle(campaign, "ELIGIBLE", evidence=evidence, url=url, verified=False, extra={"auth_resolved_at": _now()})
    return current


def recover_stale_action_running(campaign: dict[str, Any], *, stale_seconds: int = 10 * 60) -> dict[str, Any]:
    """Recover an interrupted pre-click lifecycle without pretending success.

    ACTION_RUNNING is written immediately before a click attempt. A process restart,
    verifier failure or prior bug can leave it durable forever. Once it is stale we
    move it to RETRY_WAIT, preserving the original last_attempt_at so idempotency
    cooldown continues to protect against accidental duplicate registration/clicks.
    """
    current = get_lifecycle(campaign)
    if str(current.get("state") or "").upper() != "ACTION_RUNNING":
        return current
    raw = str(current.get("last_attempt_at") or current.get("updated_at") or "")
    try:
        when = datetime.fromisoformat(raw)
        age = (datetime.now(timezone.utc) - when).total_seconds()
    except Exception:
        age = float(stale_seconds)
    if age < max(60, int(stale_seconds)):
        return current
    return update_lifecycle(
        campaign, "RETRY_WAIT",
        evidence="Recovered stale ACTION_RUNNING from an interrupted prior browser cycle; completion is unverified.",
        action=str(current.get("last_action") or ""),
        url=str(current.get("url") or ""),
        verified=False,
        extra={
            "recovered_stale_action_at": _now(),
            "last_error": "stale ACTION_RUNNING recovered safely",
        },
    )


def action_retry_allowed(campaign: dict[str, Any], *, cooldown_seconds: int = 6 * 3600, action_label: str = "") -> tuple[bool, str]:
    """Idempotency/cooldown guard for zero-fund browser actions.

    v4.6.5 is action-aware: REGISTERED blocks another Register/Join but does not prevent a later
    Claim; COMPLETED blocks a repeated Check-in/Spin but does not prevent a later Claim. With no
    label supplied, verified intermediate states are allowed to be re-inspected DOM-only.
    """
    current = get_lifecycle(campaign)
    state = str(current.get("state") or "DISCOVERED").upper()
    verified = bool(current.get("verified"))
    label = str(action_label or "").strip().lower()
    registration_action = any(x in label for x in ("register", "join", "participate", "enroll"))
    check_action = any(x in label for x in ("check in", "check-in"))
    claim_action = "claim" in label
    spin_action = "spin" in label

    if state == "CLAIMED" and verified:
        return False, "reward already claimed"
    if state == "REGISTERED" and verified:
        if registration_action:
            return False, "registration already verified"
        if not label:
            return True, "registered; DOM re-inspection allowed for downstream claim/check-in"
    if state in {"VERIFIED_COMPLETE", "COMPLETED"} and verified:
        if check_action or spin_action:
            return False, f"already {state.lower()} for this action"
        if claim_action or not label:
            return True, "completed; DOM re-inspection allowed for reward claim"
        return False, f"already {state.lower()}"

    if state in {"ACTION_RUNNING", "ACTION_SENT_UNVERIFIED", "RETRY_WAIT"}:
        raw = str(current.get("last_attempt_at") or current.get("updated_at") or "")
        try:
            when = datetime.fromisoformat(raw)
            age = (datetime.now(timezone.utc) - when).total_seconds()
            if age < max(60, int(cooldown_seconds)):
                return False, f"{state.lower()} cooldown ({int(max(0, cooldown_seconds-age))}s remaining)"
        except Exception:
            return False, f"{state.lower()} awaiting verification"
    return True, "ready"


def lifecycle_summary(campaigns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for campaign in campaigns or []:
        if not isinstance(campaign, dict):
            continue
        key = campaign_key(campaign)
        if key in seen:
            continue
        seen.add(key)
        rows.append(get_lifecycle(campaign))

    index = get_state(INDEX_KEY, [])
    if isinstance(index, list):
        for key in index[:200]:
            key = str(key)
            if not key:
                continue
            item = get_state("promo_lifecycle:" + key, {})
            if isinstance(item, dict) and item:
                item = dict(item)
                effective_key = campaign_key({
                    "campaign_key": str(item.get("campaign_key") or key),
                    "source_url": str(item.get("url") or ""),
                    "name": str(item.get("campaign") or ""),
                })
                if effective_key in seen:
                    continue
                if str(item.get("state") or "").upper() == "IN_PROGRESS":
                    item["state"] = "ACTION_SENT_UNVERIFIED"
                item["campaign_key"] = effective_key
                seen.add(effective_key)
                rows.append(item)

    counts: dict[str, int] = {}
    for item in rows:
        state = str(item.get("state") or "DISCOVERED").upper()
        counts[state] = counts.get(state, 0) + 1
    rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return {"counts": counts, "items": rows[:40], "updated_at": _now()}


def reward_audit_snapshot(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Build a truthful, zero-token campaign participation audit from persisted lifecycle state.

    The audit deliberately distinguishes *known* participation from things Stan cannot prove.
    In particular, campaign trading-volume progress is never guessed from generic account
    turnover; it remains unknown until account-side campaign evidence is available.
    """
    source = plan if isinstance(plan, dict) else {}
    campaigns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in ("automatic_trade_alignment", "human_action_required", "tracked"):
        for row in list(source.get(bucket) or []):
            if not isinstance(row, dict):
                continue
            key = campaign_key(row)
            if not key or key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item["_bucket"] = bucket
            campaigns.append(item)

    rows: list[dict[str, Any]] = []
    for campaign in campaigns:
        life = get_lifecycle(campaign)
        state = str(life.get("state") or "DISCOVERED").upper()
        verified = bool(life.get("verified"))
        actionability = str(campaign.get("actionability") or "").lower()
        blocked = actionability.startswith("not_actionable") or actionability.startswith("ineligible")
        requires_registration = bool(campaign.get("requires_registration"))
        registered = verified and state in {"REGISTERED", "COMPLETED", "VERIFIED_COMPLETE", "CLAIMED"}
        claimed = verified and state == "CLAIMED"
        checkin_complete = verified and state in {"COMPLETED", "VERIFIED_COMPLETE", "CLAIMED"} and "check" in str(life.get("last_action") or "").lower()

        if blocked:
            eligibility = "blocked_or_ineligible"
        elif state == "AUTH_REQUIRED":
            eligibility = "authentication_required"
        elif state in {"ELIGIBLE", "QUEUED", "ACTION_RUNNING", "ACTION_SENT_UNVERIFIED", "RETRY_WAIT", "REGISTERED", "COMPLETED", "VERIFIED_COMPLETE", "CLAIMED"}:
            eligibility = "authenticated_page_observed"
        else:
            eligibility = "not_yet_verified"

        if blocked:
            next_action = "none_blocked"
        elif state == "AUTH_REQUIRED":
            next_action = "manual_authorize_when_convenient"
        elif claimed:
            next_action = "none_claimed"
        elif requires_registration and not registered:
            next_action = "register_or_join_if_safe_control_visible"
        elif registered:
            next_action = "reinspect_for_checkin_or_claim"
        else:
            next_action = "reinspect_for_safe_zero_fund_action"

        volume_required = campaign.get("trading_volume_requirement_usd")
        rows.append({
            "campaign_key": campaign_key(campaign),
            "name": str(campaign.get("name") or life.get("campaign") or "Unnamed campaign")[:300],
            "bucket": str(campaign.get("_bucket") or ""),
            "actionability": str(campaign.get("actionability") or ""),
            "eligibility_status": eligibility,
            "lifecycle_state": state,
            "verified": verified,
            "registration_required": requires_registration,
            "registration_status": "not_required" if not requires_registration else ("registered" if registered else "not_verified"),
            "checkin_status": "completed" if checkin_complete else "not_verified_or_not_due",
            "claim_status": "claimed" if claimed else "not_verified_or_not_claimable",
            "trading_volume_requirement_usd": volume_required,
            "trading_volume_progress_usd": None,
            "trading_volume_progress_status": "not_verified_from_campaign_account_state" if volume_required not in (None, "", 0, 0.0) else "not_required_or_unknown",
            "natural_volume_only": True,
            "next_action": next_action,
            "last_action": str(life.get("last_action") or ""),
            "last_evidence": str(life.get("last_evidence") or "")[:800],
            "updated_at": str(life.get("updated_at") or ""),
            "source_url": str(campaign.get("source_url") or life.get("url") or "")[:1000],
        })

    return {
        "campaign_count": len(rows),
        "registered_count": sum(1 for x in rows if x.get("registration_status") == "registered"),
        "claimed_count": sum(1 for x in rows if x.get("claim_status") == "claimed"),
        "auth_required_count": sum(1 for x in rows if x.get("eligibility_status") == "authentication_required"),
        "blocked_count": sum(1 for x in rows if x.get("eligibility_status") == "blocked_or_ineligible"),
        "volume_progress_unknown_count": sum(1 for x in rows if x.get("trading_volume_progress_status") == "not_verified_from_campaign_account_state"),
        "policy": "zero-fund safe actions only; natural valid trading may count toward campaigns; no artificial volume and no fabricated progress",
        "items": rows[:80],
        "updated_at": _now(),
    }
