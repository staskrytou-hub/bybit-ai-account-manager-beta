from __future__ import annotations

from typing import Any
import hashlib

from account_os_store import set_state
from promotion_lifecycle import campaign_key
from promotion_store import latest_promotion_scan


def build_opportunity_plan(scan: dict[str, Any] | None = None, *, equity_usdt: float = 0.0) -> dict[str, Any]:
    source = scan or latest_promotion_scan() or {}
    campaigns = list(source.get("campaigns") or [])
    if not campaigns and isinstance(source.get("summary"), dict):
        campaigns = list((source.get("summary") or {}).get("campaigns") or [])

    aligned: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    track: list[dict[str, Any]] = []
    for c in campaigns:
        if not isinstance(c, dict):
            continue
        item = {
            "campaign_key": c.get("campaign_key"),
            "name": c.get("name"),
            "ends_at": c.get("ends_at"),
            "reward_type": c.get("reward_type"),
            "reward_value_estimate_usd": c.get("reward_value_estimate_usd"),
            "probabilistic": bool(c.get("probabilistic")),
            "eligible_symbols": list(c.get("eligible_symbols") or []),
            "requires_registration": bool(c.get("requires_registration")),
            "account_specific": bool(c.get("account_specific")),
            "trading_volume_requirement_usd": c.get("trading_volume_requirement_usd"),
            "actionability": str(c.get("actionability", "track")),
            "source_url": c.get("source_url"),
            "tasks": list(c.get("tasks") or []),
            "restrictions": list(c.get("restrictions") or []),
        }
        actionability = item["actionability"]
        # v4.6.2: explicit region/ineligibility blocks are tracking-only. The Browser
        # Operator must never try to register an account into a campaign that our own
        # intelligence has already classified as not actionable for this account/region.
        if actionability.startswith("not_actionable") or actionability.startswith("ineligible"):
            track.append(item)
        elif actionability == "trade_alignment" and not item["requires_registration"]:
            aligned.append(item)
        elif item["requires_registration"] or actionability.startswith("manual"):
            manual.append(item)
        else:
            track.append(item)

    plan = {
        "equity_usdt": round(float(equity_usdt or 0.0), 6),
        "automatic_trade_alignment": aligned[:20],
        "human_action_required": manual[:20],
        "tracked": track[:20],
        "rules": [
            "Never create artificial volume, self-trade, matched-trade or loosen leverage/risk for a promotion.",
            "Promotion alignment is a tie-breaker only after an independently valid trading setup passes the Risk Engine.",
            "Web-only zero-fund Register/Join/Claim/Spin/Check-in actions may be handled by the restricted Bybit Browser Operator; fund-moving tasks remain separately permission/risk-gated.",
        ],
    }
    set_state("opportunity_plan", plan)
    return plan


def merge_official_events_into_plan(plan: dict[str, Any] | None, events: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Merge fresh official Bybit event URLs into one canonical browser-inspection queue.

    v4.6.5 dedupes by canonical Bybit article identity rather than source-specific campaign keys
    or locale/query variants. Promotion Intelligence and Opportunity OS therefore cannot enqueue
    the same campaign twice just because one URL is /en/ and another is /en-US/.
    """
    current = dict(plan or {})
    tracked = [dict(x) for x in list(current.get("tracked") or []) if isinstance(x, dict)]
    manual = [dict(x) for x in list(current.get("human_action_required") or []) if isinstance(x, dict)]
    aligned = [dict(x) for x in list(current.get("automatic_trade_alignment") or []) if isinstance(x, dict)]

    seen_keys: set[str] = set()
    for bucket in (tracked, manual, aligned):
        for row in bucket:
            seen_keys.add(campaign_key(row))

    added = 0
    duplicate_skipped = 0
    for row in list(events or []):
        if not isinstance(row, dict) or not bool(row.get("official_api")):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        bucket = str(row.get("bucket") or "announcement")
        title = str(row.get("title") or "")
        text = (title + " " + str(row.get("description") or "")).lower()
        interesting = bucket in {"promotion", "alpha_prediction", "airdrop_listing"} or any(
            word in text for word in ("reward", "giveaway", "challenge", "quest", "competition", "claim", "check-in", "check in")
        )
        if not interesting:
            continue
        item = {
            # campaign_key() will canonicalize this URL across locales/query strings.
            "campaign_key": "official-" + hashlib.sha256(url.lower().encode("utf-8", errors="ignore")).hexdigest()[:28],
            "name": title[:220] or "Official Bybit event",
            "ends_at": "",
            "reward_type": bucket,
            "reward_value_estimate_usd": None,
            "probabilistic": True,
            "eligible_symbols": list(row.get("symbols") or []),
            "requires_registration": False,
            "account_specific": False,
            "trading_volume_requirement_usd": None,
            "actionability": "official_event_browser_discovery",
            "source_url": url,
            "tasks": [str(row.get("description") or "")[:500]] if str(row.get("description") or "") else [],
            "restrictions": ["Eligibility is verified on the authenticated Bybit page; Browser Operator may only use zero-fund safe controls."],
        }
        key = campaign_key(item)
        if key in seen_keys:
            duplicate_skipped += 1
            continue
        tracked.append(item)
        seen_keys.add(key)
        added += 1
        if added >= 20:
            break

    # Deduplicate any stale v4.6.4 rows already persisted in tracked itself.
    compact_tracked: list[dict[str, Any]] = []
    compact_seen: set[str] = {campaign_key(x) for x in manual + aligned}
    for row in tracked:
        key = campaign_key(row)
        if key in compact_seen:
            duplicate_skipped += 1
            continue
        compact_seen.add(key)
        compact_tracked.append(row)

    current.setdefault("equity_usdt", 0.0)
    current["automatic_trade_alignment"] = aligned
    current["human_action_required"] = manual
    current["tracked"] = compact_tracked[-40:]
    current.setdefault("rules", [
        "Never create artificial volume, self-trade, matched-trade or loosen leverage/risk for a promotion.",
        "Promotion alignment is a tie-breaker only after an independently valid trading setup passes the Risk Engine.",
        "Web-only zero-fund Register/Join/Claim/Spin/Check-in actions may be handled by the restricted Bybit Browser Operator; fund-moving tasks remain separately permission/risk-gated.",
    ])
    current["official_event_browser_queue_added"] = added
    current["official_event_duplicate_skipped"] = duplicate_skipped
    set_state("opportunity_plan", current)
    return current
