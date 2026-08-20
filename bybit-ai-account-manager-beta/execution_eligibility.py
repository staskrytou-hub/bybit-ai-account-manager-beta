from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from trading_store import get_state, recent_assessments, set_state

STATE_KEY = "exchange_execution_restrictions_v465"
FAMILY_PREFIX = "__family__:"
_BOOTSTRAPPED = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict[str, Any]:
    raw = get_state(STATE_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    # Keep the newest/most relevant records only; this is observability state, not trade history.
    rows = list(data.items())
    rows.sort(key=lambda kv: float((kv[1] or {}).get("last_seen_ts", 0.0) or 0.0), reverse=True)
    set_state(STATE_KEY, json.dumps(dict(rows[:120]), ensure_ascii=False, separators=(",", ":")))






def _persistent_block(item: dict[str, Any] | None) -> bool:
    row = item if isinstance(item, dict) else {}
    return bool(row.get("persistent")) or str(row.get("class") or "").lower() == "agreement_required"


def _item_blocked(item: dict[str, Any] | None, now: float | None = None) -> bool:
    row = item if isinstance(item, dict) else {}
    if _persistent_block(row):
        return True
    return float(row.get("blocked_until_ts", 0.0) or 0.0) > float(now if now is not None else time.time())


def instrument_restriction_family(instrument: dict[str, Any] | None) -> str:
    """Return the narrow Trading-Terms family supported by current Bybit documentation.

    Bybit TradFi perpetuals cover equities/ETFs/commodities; observed stock/ETF agreement rejects are shared with the stock/metal gate,
    while oil uses a separate agreement. Forex is a TradFi symbolType but is not folded into
    either agreement family without its own exchange rejection. This avoids blocking valid
    opportunities more broadly than the exchange evidence supports.
    """
    info = instrument if isinstance(instrument, dict) else {}
    symbol_type = str(info.get("symbolType") or "").strip().lower()
    symbol_text = " ".join(
        str(info.get(k) or "") for k in ("symbol", "baseCoin", "displayName")
    ).upper()
    if symbol_type in {"stock", "etf"}:
        return "tradfi_stock_metal"
    if symbol_type == "commodity":
        if any(token in symbol_text for token in ("XAU", "XAG", "GOLD", "SILVER")):
            return "tradfi_stock_metal"
        if any(token in symbol_text for token in ("CLUSDT", "WTI", "CRUDE", "OIL")):
            return "tradfi_oil"
        # Unknown commodity agreements stay in their own narrow family instead of inheriting
        # a stock or oil gate without evidence.
        return "tradfi_commodity_other"
    return ""


def _family_key(family: str) -> str:
    return f"{FAMILY_PREFIX}{str(family or '').strip().lower()}"


def family_restriction(family: str) -> dict[str, Any]:
    family = str(family or "").strip().lower()
    if not family:
        return {"family": "", "blocked": False}
    item = _load().get(_family_key(family))
    if not isinstance(item, dict):
        return {"family": family, "blocked": False}
    result = dict(item)
    result["blocked"] = _item_blocked(result)
    return result


def mark_family_restriction(
    family: str, error_text: str, *, source_symbol: str = "", source: str = "order_reject",
) -> dict[str, Any] | None:
    family = str(family or "").strip().lower()
    if not family:
        return None
    classified = classify_exchange_restriction(error_text)
    # Family-wide propagation is deliberately narrow. Only Trading-Terms/agreement rejects
    # are propagated only within the documented matching TradFi agreement family. Other exchange
    # errors remain symbol-scoped unless Bybit explicitly reports otherwise.
    if not classified or str(classified.get("class")) != "agreement_required":
        return None
    now = time.time()
    data = _load()
    key = _family_key(family)
    prior = data.get(key) if isinstance(data.get(key), dict) else {}
    prior_until = float(prior.get("blocked_until_ts", 0.0) or 0.0)
    candidate_until = now + int(classified.get("block_seconds", 24 * 3600) or 24 * 3600)
    blocked_until = prior_until if prior_until > now else candidate_until
    item = {
        "scope": "family",
        "family": family,
        "symbol": str(source_symbol or "").upper(),
        "blocked": True,
        "class": "agreement_required",
        "code": int(classified.get("code", 0) or 0),
        "reason": (
            f"Bybit Trading Terms are not accepted for the {family} agreement family; "
            "matching contracts are blocked before paid AI until the agreement is accepted."
        ),
        "exchange_error": str(error_text or "")[:1200],
        "human_action_required": True,
        "persistent": True,
        "release_policy": "blocked until real exchange success/manual Trading Terms acceptance is confirmed; TTL never auto-unblocks",
        "recheck_after_ts": blocked_until,
        "first_seen_at": str(prior.get("first_seen_at") or _now_iso()),
        "last_seen_at": _now_iso(),
        "last_seen_ts": now,
        "blocked_until_ts": blocked_until,
        "blocked_until": datetime.fromtimestamp(blocked_until, tz=timezone.utc).isoformat(timespec="seconds"),
        "source": str(source or "order_reject")[:80],
    }
    data[key] = item
    _save(data)
    return item


def symbol_or_family_restriction(symbol: str, instrument: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the strongest active exchange eligibility block without using paid AI."""
    direct = symbol_restriction(symbol)
    family = instrument_restriction_family(instrument)
    if family:
        # Migration bridge: v4.6.4 may have persisted only the rejected symbol. Once v4.6.5
        # sees that symbol's public instrument metadata, promote its agreement gate only to
        # the documented matching agreement family so sibling contracts are not rediscovered.
        if bool(direct.get("blocked")) and str(direct.get("class")) == "agreement_required":
            mark_family_restriction(
                family, str(direct.get("exchange_error") or direct.get("reason") or "agreement required"),
                source_symbol=symbol, source="symbol_restriction_family_promotion",
            )
        fam = family_restriction(family)
        if bool(fam.get("blocked")):
            result = dict(fam)
            result["symbol"] = str(symbol or "").upper()
            result["matched_family"] = family
            return result
    return direct


def classify_exchange_restriction(error_text: str) -> dict[str, Any] | None:
    text = str(error_text or "")
    low = text.lower()
    match = re.search(r"retcode\s*=\s*(\d+)", low)
    code = int(match.group(1)) if match else 0

    # These blocks are deterministic account/product eligibility failures. Re-running paid AI
    # cannot change them, so the symbol is temporarily removed from the paid funnel.
    if code in {110123, 110125, 110126} or "sign the required agreement" in low or "agree to the trading terms" in low or "agreement before trading" in low:
        return {
            "code": code or 110126,
            "class": "agreement_required",
            "block_seconds": 24 * 3600,  # local recheck cadence only; v4.6.7 keeps this restriction persistent
            "human_action_required": True,
            "reason": "Bybit trading agreement/Trading Terms must be accepted before this contract can be traded.",
        }
    if code == 110124 or "cooling-off" in low or "cooling off" in low:
        return {
            "code": code,
            "class": "cooling_off",
            "block_seconds": 6 * 3600,
            "human_action_required": False,
            "reason": "Bybit cooling-off restriction is active for this contract/account.",
        }
    if code == 110132 or "not available in your region" in low or "product not available in your region" in low:
        return {
            "code": code,
            "class": "region_restricted",
            "block_seconds": 30 * 24 * 3600,
            "human_action_required": False,
            "reason": "Bybit reports this product is not available in the account region.",
        }
    if code == 110136 or "whitelisted accounts only" in low or "required access" in low:
        return {
            "code": code,
            "class": "access_not_enabled",
            "block_seconds": 24 * 3600,
            "human_action_required": True,
            "reason": "Bybit reports this contract requires account access/whitelisting that is not enabled.",
        }
    if code == 110137 or "reduce-only window" in low:
        return {
            "code": code,
            "class": "reduce_only_window",
            "block_seconds": 12 * 3600,
            "human_action_required": False,
            "reason": "Bybit contract is in a reduce-only window; new entries are unavailable.",
        }
    return None


def mark_symbol_restriction(symbol: str, error_text: str, *, source: str = "order_reject") -> dict[str, Any] | None:
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return None
    classified = classify_exchange_restriction(error_text)
    if not classified:
        return None
    now = time.time()
    data = _load()
    prior = data.get(symbol) if isinstance(data.get(symbol), dict) else {}
    prior_until = float(prior.get("blocked_until_ts", 0.0) or 0.0)
    candidate_until = now + int(classified.get("block_seconds", 3600) or 3600)
    # A restart/bootstrap must not endlessly extend an already-known block.
    blocked_until = prior_until if prior_until > now else candidate_until
    first_seen = str(prior.get("first_seen_at") or _now_iso())
    item = {
        "symbol": symbol,
        "blocked": True,
        "class": str(classified.get("class") or "exchange_restriction"),
        "code": int(classified.get("code", 0) or 0),
        "reason": str(classified.get("reason") or error_text)[:600],
        "exchange_error": str(error_text or "")[:1200],
        "human_action_required": bool(classified.get("human_action_required")),
        "persistent": str(classified.get("class") or "") == "agreement_required",
        "release_policy": "blocked until real exchange success/manual Trading Terms acceptance is confirmed; TTL never auto-unblocks" if str(classified.get("class") or "") == "agreement_required" else "time-based retry",
        "recheck_after_ts": blocked_until,
        "first_seen_at": first_seen,
        "last_seen_at": _now_iso(),
        "last_seen_ts": now,
        "blocked_until_ts": blocked_until,
        "blocked_until": datetime.fromtimestamp(blocked_until, tz=timezone.utc).isoformat(timespec="seconds"),
        "source": str(source or "order_reject")[:80],
    }
    data[symbol] = item
    _save(data)
    return item


def symbol_restriction(symbol: str) -> dict[str, Any]:
    symbol = str(symbol or "").upper().strip()
    item = _load().get(symbol)
    if not isinstance(item, dict):
        return {"symbol": symbol, "blocked": False}
    result = dict(item)
    result["blocked"] = _item_blocked(result)
    return result


def execution_restrictions() -> dict[str, Any]:
    data = _load()
    now = time.time()
    active: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for symbol, raw in data.items():
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if str(symbol).startswith(FAMILY_PREFIX):
            item.setdefault("scope", "family")
            item.setdefault("family", str(symbol)[len(FAMILY_PREFIX):])
        else:
            item.setdefault("scope", "symbol")
            item.setdefault("symbol", str(symbol).upper())
        item["blocked"] = _item_blocked(item, now)
        (active if item["blocked"] else expired).append(item)
    active.sort(key=lambda x: float(x.get("blocked_until_ts", 0.0) or 0.0), reverse=True)
    expired.sort(key=lambda x: float(x.get("last_seen_ts", 0.0) or 0.0), reverse=True)
    return {
        "active_count": len(active),
        "active": active[:30],
        "expired": expired[:10],
        "updated_at": _now_iso(),
    }


def bootstrap_from_recent_assessments(limit: int = 80) -> dict[str, Any]:
    """Learn deterministic exchange eligibility failures from persisted live execution history.

    This is intentionally local/no-token. It lets an upgrade immediately remember a 11012x/region
    rejection from the previous release instead of paying the model to rediscover the same block.
    The database migration scan runs once per process; new live rejects are recorded immediately.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        status = execution_restrictions()
        status["learned_from_history"] = 0
        status["bootstrap_cached"] = True
        return status
    _BOOTSTRAPPED = True
    learned = 0
    for row in recent_assessments(limit=max(1, min(int(limit), 200))):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
        confirmation = execution.get("confirmation") if isinstance(execution.get("confirmation"), dict) else {}
        text = " | ".join(
            x for x in (
                str(execution.get("submit_error") or ""),
                str(execution.get("error") or ""),
                str(confirmation.get("reason") or ""),
            ) if x
        )
        if symbol and classify_exchange_restriction(text):
            if mark_symbol_restriction(symbol, text, source="persisted_execution_history"):
                learned += 1
    status = execution_restrictions()
    status["learned_from_history"] = learned
    return status
