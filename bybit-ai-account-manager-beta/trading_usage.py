from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from paths import TRADING_DB
from runtime_control import runtime_stop_requested

_PROVIDER_ROW_ID = 1
_PROVIDER_PROBE_STALE_SECONDS = 120


def _iso_from_ts(value: float) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(TRADING_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_usage(ts TEXT NOT NULL, tokens INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'unknown')"
    )
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(ai_usage)").fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE ai_usage ADD COLUMN kind TEXT NOT NULL DEFAULT 'unknown'")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_gate(key TEXT PRIMARY KEY, last_ts REAL NOT NULL DEFAULT 0, signature TEXT NOT NULL DEFAULT '', calls INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_provider_guard(
          id INTEGER PRIMARY KEY CHECK(id=1),
          state TEXT NOT NULL DEFAULT 'ACTIVE',
          code TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '',
          last_error TEXT NOT NULL DEFAULT '',
          tripped_at TEXT NOT NULL DEFAULT '',
          next_probe_ts REAL NOT NULL DEFAULT 0,
          probe_claimed_ts REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO ai_provider_guard(id,state,code,reason,last_error,tripped_at,next_probe_ts,probe_claimed_ts) VALUES(1,'ACTIVE','','','','',0,0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_budget_epoch(id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT '', baseline_rowid INTEGER NOT NULL DEFAULT 0)"
    )
    epoch_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(ai_budget_epoch)").fetchall()}
    if "baseline_rowid" not in epoch_cols:
        conn.execute("ALTER TABLE ai_budget_epoch ADD COLUMN baseline_rowid INTEGER NOT NULL DEFAULT 0")
    conn.execute("INSERT OR IGNORE INTO ai_budget_epoch(id,version,started_at,baseline_rowid) VALUES(1,'','',0)")
    conn.commit()
    return conn


def record_trading_tokens(tokens: int, kind: str = "unknown") -> None:
    value = max(0, int(tokens or 0))
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ai_usage(ts,tokens,kind) VALUES(?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), value, str(kind or "unknown")[:80]),
        )


def trading_tokens_today(kind: str | None = None) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        if kind:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM ai_usage WHERE substr(ts,1,10)=? AND kind=?",
                (day, str(kind)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM ai_usage WHERE substr(ts,1,10)=?",
                (day,),
            ).fetchone()
    return int(row[0] or 0)


def trading_ai_calls_today(kind: str | None = None) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        if kind:
            row = conn.execute(
                "SELECT COUNT(*) FROM ai_usage WHERE substr(ts,1,10)=? AND kind=?",
                (day, str(kind)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM ai_usage WHERE substr(ts,1,10)=?",
                (day,),
            ).fetchone()
    return int(row[0] or 0)


def usage_by_kind_today() -> dict[str, dict[str, int]]:
    day = datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS calls, COALESCE(SUM(tokens),0) AS tokens FROM ai_usage WHERE substr(ts,1,10)=? GROUP BY kind ORDER BY tokens DESC",
            (day,),
        ).fetchall()
    return {str(r["kind"]): {"calls": int(r["calls"]), "tokens": int(r["tokens"])} for r in rows}


def ensure_budget_epoch(version: str) -> dict[str, Any]:
    """Start a new per-version AI budget accounting epoch once.

    Historical same-day calls from an older Stan release remain visible in total usage, but
    do not consume the newly installed release's per-kind call/token ceilings. A row-id
    baseline avoids same-second timestamp ambiguity during upgrade.
    """
    version = str(version or "").strip()[:40]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version,started_at,baseline_rowid FROM ai_budget_epoch WHERE id=1").fetchone()
        current = str(row["version"] or "") if row else ""
        started = str(row["started_at"] or "") if row else ""
        baseline = int(row["baseline_rowid"] or 0) if row else 0
        if current != version:
            maxrow = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM ai_usage").fetchone()
            baseline = int(maxrow[0] or 0)
            conn.execute("UPDATE ai_budget_epoch SET version=?, started_at=?, baseline_rowid=? WHERE id=1", (version, now_iso, baseline))
            conn.commit()
            return {"version": version, "started_at": now_iso, "baseline_rowid": baseline}
        conn.commit()
        return {"version": current, "started_at": started, "baseline_rowid": baseline}



def ensure_budget_epoch_compatible(version: str, compatible_versions: set[str] | list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Adopt a new release label without resetting today's existing budget baseline.

    Used for hotfixes whose purpose is to reduce waste, not grant another same-day allowance.
    If the installed epoch is from a compatible release, only the version label is updated;
    baseline_rowid and started_at stay unchanged. Otherwise a normal fresh epoch is created.
    """
    version = str(version or "").strip()[:40]
    compatible = {str(x or "").strip()[:40] for x in compatible_versions}
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version,started_at,baseline_rowid FROM ai_budget_epoch WHERE id=1").fetchone()
        current = str(row["version"] or "") if row else ""
        started = str(row["started_at"] or "") if row else ""
        baseline = int(row["baseline_rowid"] or 0) if row else 0
        if current == version:
            conn.commit()
            return {"version": current, "started_at": started, "baseline_rowid": baseline, "carried_forward": True}
        if current in compatible and baseline >= 0:
            conn.execute("UPDATE ai_budget_epoch SET version=? WHERE id=1", (version,))
            conn.commit()
            return {"version": version, "started_at": started, "baseline_rowid": baseline, "carried_forward": True, "from_version": current}
        conn.commit()
    result = ensure_budget_epoch(version)
    result["carried_forward"] = False
    return result

def budget_epoch_status() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT version,started_at,baseline_rowid FROM ai_budget_epoch WHERE id=1").fetchone()
    return {
        "version": str(row["version"] or "") if row else "",
        "started_at": str(row["started_at"] or "") if row else "",
        "baseline_rowid": int(row["baseline_rowid"] or 0) if row else 0,
    }


def _budget_baseline_rowid() -> int:
    return int(budget_epoch_status().get("baseline_rowid", 0) or 0)


def budgeted_trading_tokens_today(kind: str | None = None) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    baseline = _budget_baseline_rowid()
    with _connect() as conn:
        if kind:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM ai_usage WHERE substr(ts,1,10)=? AND rowid>? AND kind=?",
                (day, baseline, str(kind)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM ai_usage WHERE substr(ts,1,10)=? AND rowid>?",
                (day, baseline),
            ).fetchone()
    return int(row[0] or 0)


def budgeted_ai_calls_today(kind: str | None = None) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    baseline = _budget_baseline_rowid()
    with _connect() as conn:
        if kind:
            row = conn.execute(
                "SELECT COUNT(*) FROM ai_usage WHERE substr(ts,1,10)=? AND rowid>? AND kind=?",
                (day, baseline, str(kind)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM ai_usage WHERE substr(ts,1,10)=? AND rowid>?",
                (day, baseline),
            ).fetchone()
    return int(row[0] or 0)


def budgeted_usage_by_kind_today() -> dict[str, dict[str, int]]:
    day = datetime.now(timezone.utc).date().isoformat()
    baseline = _budget_baseline_rowid()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS calls, COALESCE(SUM(tokens),0) AS tokens FROM ai_usage WHERE substr(ts,1,10)=? AND rowid>? GROUP BY kind ORDER BY tokens DESC",
            (day, baseline),
        ).fetchall()
    return {str(r["kind"]): {"calls": int(r["calls"]), "tokens": int(r["tokens"])} for r in rows}


def provider_guard_status() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM ai_provider_guard WHERE id=?", (_PROVIDER_ROW_ID,)).fetchone()
    if not row:
        return {"state": "ACTIVE", "paused": False, "code": "", "reason": "", "last_error": "", "tripped_at": "", "next_probe_at": ""}
    state = str(row["state"] or "ACTIVE").upper()
    return {
        "state": state,
        "paused": state in {"PAUSED", "PROBING"},
        "code": str(row["code"] or ""),
        "reason": str(row["reason"] or ""),
        "last_error": str(row["last_error"] or ""),
        "tripped_at": str(row["tripped_at"] or ""),
        "next_probe_at": _iso_from_ts(float(row["next_probe_ts"] or 0)),
        "probe_in_flight": state == "PROBING",
    }


def trip_provider_guard(*, code: str, reason: str, error: str, probe_after_seconds: int = 900) -> dict[str, Any]:
    now = time.time()
    next_probe = now + max(60, int(probe_after_seconds or 900))
    tripped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "UPDATE ai_provider_guard SET state='PAUSED', code=?, reason=?, last_error=?, tripped_at=?, next_probe_ts=?, probe_claimed_ts=0 WHERE id=?",
            (str(code or "provider_unavailable")[:120], str(reason or "AI provider unavailable")[:300], str(error or "")[:1800], tripped_at, next_probe, _PROVIDER_ROW_ID),
        )
    return provider_guard_status()


def clear_provider_guard() -> None:
    """Administrative/test reset of the provider circuit."""
    with _connect() as conn:
        conn.execute(
            "UPDATE ai_provider_guard SET state='ACTIVE', code='', reason='', last_error='', tripped_at='', next_probe_ts=0, probe_claimed_ts=0 WHERE id=?",
            (_PROVIDER_ROW_ID,),
        )


def provider_call_succeeded() -> None:
    """Close the circuit only when this call was the admitted recovery probe.

    A normal in-flight request that began before another thread tripped the billing circuit
    must not accidentally clear that circuit when it finishes later.
    """
    with _connect() as conn:
        row = conn.execute("SELECT state FROM ai_provider_guard WHERE id=?", (_PROVIDER_ROW_ID,)).fetchone()
        if row and str(row["state"] or "").upper() == "PROBING":
            conn.execute(
                "UPDATE ai_provider_guard SET state='ACTIVE', code='', reason='', last_error='', tripped_at='', next_probe_ts=0, probe_claimed_ts=0 WHERE id=?",
                (_PROVIDER_ROW_ID,),
            )


def request_provider_probe() -> None:
    """Allow the next user-forced AI action to test whether provider access recovered."""
    with _connect() as conn:
        row = conn.execute("SELECT state FROM ai_provider_guard WHERE id=?", (_PROVIDER_ROW_ID,)).fetchone()
        state = str(row["state"] or "ACTIVE").upper() if row else "ACTIVE"
        if state != "ACTIVE":
            conn.execute(
                "UPDATE ai_provider_guard SET state='PAUSED', next_probe_ts=0, probe_claimed_ts=0 WHERE id=?",
                (_PROVIDER_ROW_ID,),
            )


def provider_reservation_allowed() -> tuple[bool, str]:
    status = provider_guard_status()
    state = str(status.get("state") or "ACTIVE").upper()
    if state == "ACTIVE":
        return True, "provider active"
    if state == "PROBING":
        return False, "AI provider recovery probe already in flight"
    with _connect() as conn:
        row = conn.execute("SELECT next_probe_ts FROM ai_provider_guard WHERE id=?", (_PROVIDER_ROW_ID,)).fetchone()
    next_probe = float(row["next_probe_ts"] or 0) if row else 0.0
    if next_probe <= time.time():
        return True, "AI provider recovery probe is due"
    reason = str(status.get("reason") or status.get("code") or "AI provider paused")
    return False, f"AI provider paused: {reason}; next probe {status.get('next_probe_at') or 'later'}"


def begin_provider_call(kind: str = "model") -> tuple[bool, str]:
    """Atomically admit at most one recovery probe while the provider circuit is paused."""
    now = time.time()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM ai_provider_guard WHERE id=?", (_PROVIDER_ROW_ID,)).fetchone()
        state = str(row["state"] or "ACTIVE").upper() if row else "ACTIVE"
        if state == "ACTIVE":
            conn.commit()
            return True, "provider active"
        next_probe = float(row["next_probe_ts"] or 0) if row else 0.0
        claimed = float(row["probe_claimed_ts"] or 0) if row else 0.0
        if state == "PROBING" and claimed and now - claimed < _PROVIDER_PROBE_STALE_SECONDS:
            conn.commit()
            return False, "AI provider recovery probe already in flight"
        if state == "PAUSED" and next_probe > now:
            reason = str(row["reason"] or row["code"] or "AI provider paused")
            conn.commit()
            return False, f"AI provider paused: {reason}"
        conn.execute(
            "UPDATE ai_provider_guard SET state='PROBING', probe_claimed_ts=? WHERE id=?",
            (now, _PROVIDER_ROW_ID),
        )
        conn.commit()
        return True, f"provider recovery probe admitted ({kind})"


def release_ai_reservation(cooldown_key: str) -> None:
    """Undo same-state cooldown after a provider/billing outage; market evidence itself did not fail."""
    key = str(cooldown_key or "")[:180]
    if not key:
        return
    with _connect() as conn:
        conn.execute("UPDATE ai_gate SET last_ts=0, signature='' WHERE key=?", (key,))


def paced_daily_call_cap(max_calls: int, *, lane: str = "normal", now: datetime | None = None, window_hours: int = 4) -> dict[str, Any]:
    """Return a smooth UTC pacing cap without changing the daily allowance.

    v4.6.8 replaces the coarse 4-hour stair-step with continuous time-based unlocking.
    The daily maximum is unchanged.  The normal lane starts with a modest working buffer;
    the reserve lane starts smaller and grows through the UTC day.  This prevents an early
    burn while avoiding multi-hour dead zones just because a fixed window boundary has not
    arrived yet.
    """
    import math

    total = max(0, int(max_calls or 0))
    hours = max(1, min(12, int(window_hours or 4)))  # kept for backward-compatible telemetry
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    if total <= 0:
        return {
            "enabled": False, "daily_max_calls": total, "paced_max_calls": 0,
            "window_hours": hours, "utc_hour": current.hour, "next_unlock_at": "",
            "pacing_model": "smooth_v468",
        }

    elapsed = current.hour * 3600 + current.minute * 60 + current.second
    progress = max(0.0, min(0.999999, elapsed / 86400.0))
    reserve = str(lane).lower() == "reserve"
    start_fraction = 0.20 if reserve else 0.35
    unlocked_fraction = start_fraction + (1.0 - start_fraction) * progress
    paced = max(1, min(total, int(math.ceil(total * unlocked_fraction - 1e-12))))

    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if paced >= total:
        next_unlock = "next UTC day"
    else:
        # ceil(total*f) becomes paced+1 once total*f exceeds paced.
        needed_fraction = min(1.0, (paced / float(total)) + 1e-9)
        needed_progress = max(0.0, min(1.0, (needed_fraction - start_fraction) / max(1e-9, 1.0 - start_fraction)))
        boundary = midnight + timedelta(seconds=int(needed_progress * 86400))
        if boundary <= current:
            boundary = current + timedelta(minutes=15)
        next_unlock = boundary.isoformat(timespec="minutes")

    return {
        "enabled": True,
        "lane": str(lane),
        "daily_max_calls": total,
        "paced_max_calls": paced,
        "base_paced_max_calls": paced,
        "window_hours": hours,
        "utc_hour": current.hour,
        "day_progress": round(progress, 4),
        "unlocked_fraction": round(unlocked_fraction, 4),
        "next_unlock_at": next_unlock,
        "pacing_model": "smooth_v468",
    }



def legacy_paced_daily_call_cap(max_calls: int, *, lane: str = "normal", now: datetime | None = None, window_hours: int = 4) -> dict[str, Any]:
    """v4.6.7 stair-step pacing retained for Spot to avoid changing an unbroken lane."""
    import math
    total = max(0, int(max_calls or 0))
    hours = max(1, min(12, int(window_hours or 4)))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    if total <= 0:
        return {"enabled": False, "daily_max_calls": total, "paced_max_calls": 0, "window_hours": hours, "utc_hour": current.hour, "next_unlock_at": "", "pacing_model": "legacy_v467"}
    normal_curve = (0.40, 0.60, 0.70, 0.80, 0.90, 1.00)
    reserve_curve = (0.25, 0.375, 0.50, 0.625, 0.75, 1.00)
    curve = reserve_curve if str(lane).lower() == "reserve" else normal_curve
    elapsed = current.hour * 3600 + current.minute * 60 + current.second
    progress = max(0.0, min(0.999999, elapsed / 86400.0))
    idx = min(len(curve) - 1, int(progress * len(curve)))
    paced = max(1, min(total, int(math.ceil(total * curve[idx]))))
    next_idx = min(len(curve) - 1, idx + 1)
    if next_idx == idx:
        next_unlock = "next UTC day"
    else:
        boundary_seconds = int((next_idx / len(curve)) * 86400)
        boundary = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=boundary_seconds)
        next_unlock = boundary.isoformat(timespec="minutes")
    return {
        "enabled": True, "lane": str(lane), "daily_max_calls": total,
        "paced_max_calls": paced, "window_hours": hours, "utc_hour": current.hour,
        "curve_index": idx, "next_unlock_at": next_unlock, "pacing_model": "legacy_v467",
    }

def opportunity_market_heat(snapshot: dict[str, Any] | None) -> dict[str, float]:
    """Deterministic market-activity score used only to unlock scarce AI verification.

    This score never creates a trade and never weakens Risk Engine requirements.  It only
    decides whether a very strong deterministic proposal may borrow a small number of calls
    from later in the SAME UTC-day allowance.
    """
    snap = dict(snapshot or {})

    def _f(key: str) -> float:
        try:
            return abs(float(snap.get(key, 0.0) or 0.0))
        except Exception:
            return 0.0

    impulse = min(1.0, _f("ticker_24h_pct") / 8.0)
    atr_heat = min(1.0, _f("atr_pct") / 1.50)
    realized = min(1.0, _f("realized_vol_20_pct") / 1.00)
    # Require either a broad impulse or meaningful short-horizon activity.  Taking the max
    # avoids declaring an active breakout "quiet" just because one volatility estimator lags.
    short_horizon = 0.60 * atr_heat + 0.40 * realized
    heat = max(impulse, short_horizon)
    return {
        "heat": round(max(0.0, min(1.0, heat)), 4),
        "impulse_component": round(impulse, 4),
        "atr_component": round(atr_heat, 4),
        "realized_component": round(realized, 4),
    }


def opportunity_aware_paced_call_cap(
    max_calls: int,
    *,
    lane: str = "normal",
    snapshot: dict[str, Any] | None = None,
    proposal_quality: float = 0.0,
    proposal_setup: float = 0.0,
    now: datetime | None = None,
    window_hours: int = 4,
    borrow_calls: int = 1,
    borrow_min_quality: float = 0.84,
    borrow_min_setup: float = 0.68,
    borrow_min_heat: float = 0.65,
    exceptional_quality: float = 0.92,
) -> dict[str, Any]:
    """Return the paced cap plus a bounded same-day opportunity borrow.

    Borrowing never raises ``daily_max_calls``.  At most ``borrow_calls`` are unlocked early,
    and only for a strong deterministic proposal in an active market (or an exceptional
    proposal).  This is the v4.6.8 escape hatch for daytime volatility without reverting to
    the v4.6.6 all-morning budget burn.
    """
    base = paced_daily_call_cap(max_calls, lane=lane, now=now, window_hours=window_hours)
    total = int(base.get("daily_max_calls", 0) or 0)
    base_cap = int(base.get("paced_max_calls", 0) or 0)
    q = max(0.0, float(proposal_quality or 0.0))
    setup = max(0.0, float(proposal_setup or 0.0))
    heat = opportunity_market_heat(snapshot)
    heat_value = float(heat.get("heat", 0.0) or 0.0)
    allowed = (
        total > 0
        and int(borrow_calls or 0) > 0
        and q >= float(borrow_min_quality)
        and setup >= float(borrow_min_setup)
        and (heat_value >= float(borrow_min_heat) or q >= float(exceptional_quality))
    )
    borrowed = min(max(0, int(borrow_calls or 0)), max(0, total - base_cap)) if allowed else 0
    effective = min(total, base_cap + borrowed)
    out = dict(base)
    out.update({
        "base_paced_max_calls": base_cap,
        "paced_max_calls": effective,
        "opportunity_borrow_active": bool(borrowed > 0),
        "opportunity_borrow_calls": borrowed,
        "opportunity_quality": round(q, 4),
        "opportunity_setup": round(setup, 4),
        "opportunity_heat": heat_value,
        "opportunity_heat_components": heat,
        "borrow_min_quality": float(borrow_min_quality),
        "borrow_min_setup": float(borrow_min_setup),
        "borrow_min_heat": float(borrow_min_heat),
        "exceptional_quality": float(exceptional_quality),
        "policy": "bounded same-UTC-day borrow; daily call/token caps unchanged",
    })
    return out


def _session_curve_fraction(current: datetime, lane: str) -> tuple[float, str]:
    """v4.6.9 UTC session curve: preserve overnight calls for London/New York activity."""
    hour = current.hour + current.minute / 60.0 + current.second / 3600.0
    reserve = str(lane).lower() == "reserve"
    # More conservative overnight than v4.6.8; most verification capacity is released
    # from London open through the New York session.  Daily maxima are unchanged.
    points = (
        ((0.0, 0.05), (6.0, 0.15), (12.0, 0.50), (16.0, 0.80), (20.0, 1.00), (24.0, 1.00))
        if reserve else
        ((0.0, 0.10), (6.0, 0.25), (12.0, 0.65), (16.0, 0.90), (20.0, 1.00), (24.0, 1.00))
    )
    phase = "overnight"
    if 6.0 <= hour < 12.0:
        phase = "london"
    elif 12.0 <= hour < 16.0:
        phase = "london_ny_overlap"
    elif 16.0 <= hour < 21.0:
        phase = "new_york"
    elif hour >= 21.0:
        phase = "late_day"
    for (h0, f0), (h1, f1) in zip(points, points[1:]):
        if h0 <= hour <= h1:
            span = max(1e-9, h1 - h0)
            frac = f0 + (f1 - f0) * ((hour - h0) / span)
            return max(0.0, min(1.0, frac)), phase
    return 1.0, phase


def session_priority_paced_call_cap(max_calls: int, *, lane: str = "normal", now: datetime | None = None, window_hours: int = 4) -> dict[str, Any]:
    """Day-session-first pacing for v4.6.9.

    Unlike v4.6.8, this deliberately preserves most calls overnight and releases them into
    London/New York activity.  It does not change the daily call or token budget.
    """
    import math
    total = max(0, int(max_calls or 0))
    hours = max(1, min(12, int(window_hours or 4)))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    if total <= 0:
        return {
            "enabled": False, "daily_max_calls": total, "paced_max_calls": 0,
            "window_hours": hours, "utc_hour": current.hour, "next_unlock_at": "",
            "pacing_model": "session_priority_v469", "session_phase": "disabled",
        }
    unlocked_fraction, phase = _session_curve_fraction(current, lane)
    paced = max(1, min(total, int(math.ceil(total * unlocked_fraction - 1e-12))))

    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if paced >= total:
        next_unlock = "next UTC day"
    else:
        # Find the time when the continuous session curve first exceeds paced/total.
        target = min(1.0, paced / float(total) + 1e-9)
        points = (
            ((0.0, 0.05), (6.0, 0.15), (12.0, 0.50), (16.0, 0.80), (20.0, 1.00), (24.0, 1.00))
            if str(lane).lower() == "reserve" else
            ((0.0, 0.10), (6.0, 0.25), (12.0, 0.65), (16.0, 0.90), (20.0, 1.00), (24.0, 1.00))
        )
        boundary_hour = 24.0
        for (h0, f0), (h1, f1) in zip(points, points[1:]):
            if target <= f1 + 1e-12 and f1 > f0:
                ratio = max(0.0, min(1.0, (target - f0) / (f1 - f0)))
                boundary_hour = h0 + (h1 - h0) * ratio
                break
        boundary = midnight + timedelta(seconds=int(boundary_hour * 3600))
        if boundary <= current:
            boundary = current + timedelta(minutes=15)
        next_unlock = boundary.isoformat(timespec="minutes")
    return {
        "enabled": True, "lane": str(lane), "daily_max_calls": total,
        "paced_max_calls": paced, "base_paced_max_calls": paced,
        "window_hours": hours, "utc_hour": current.hour,
        "unlocked_fraction": round(unlocked_fraction, 4), "next_unlock_at": next_unlock,
        "pacing_model": "session_priority_v469", "session_phase": phase,
        "overnight_budget_preservation": True,
    }


def session_opportunity_aware_paced_call_cap(
    max_calls: int,
    *,
    lane: str = "normal",
    snapshot: dict[str, Any] | None = None,
    proposal_quality: float = 0.0,
    proposal_setup: float = 0.0,
    now: datetime | None = None,
    window_hours: int = 4,
    borrow_calls: int = 1,
    borrow_min_quality: float = 0.84,
    borrow_min_setup: float = 0.68,
    borrow_min_heat: float = 0.65,
    exceptional_quality: float = 0.92,
    day_session_start_utc: int = 6,
    day_session_end_utc: int = 21,
    exceptional_burst_calls: int = 1,
) -> dict[str, Any]:
    """v4.6.9 opportunity gate with day-session continuity.

    Strong proposals keep the v4.6.8 one-call borrow.  Exceptional proposals during the
    London/New York trading day may unlock one *additional* future call, still capped by the
    same daily allowance.  This prevents a q~0.95 setup from waiting hours behind a clock.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    base = session_priority_paced_call_cap(max_calls, lane=lane, now=current, window_hours=window_hours)
    total = int(base.get("daily_max_calls", 0) or 0)
    base_cap = int(base.get("paced_max_calls", 0) or 0)
    q = max(0.0, float(proposal_quality or 0.0))
    setup = max(0.0, float(proposal_setup or 0.0))
    heat = opportunity_market_heat(snapshot)
    heat_value = float(heat.get("heat", 0.0) or 0.0)
    strong = (
        total > 0 and int(borrow_calls or 0) > 0 and q >= float(borrow_min_quality)
        and setup >= float(borrow_min_setup)
        and (heat_value >= float(borrow_min_heat) or q >= float(exceptional_quality))
    )
    borrowed = min(max(0, int(borrow_calls or 0)), max(0, total - base_cap)) if strong else 0
    after_borrow = min(total, base_cap + borrowed)
    day_active = int(day_session_start_utc) <= current.hour < int(day_session_end_utc)
    exceptional = (
        day_active and q >= float(exceptional_quality) and setup >= float(borrow_min_setup)
        and heat_value >= float(borrow_min_heat)
    )
    burst = min(max(0, int(exceptional_burst_calls or 0)), max(0, total - after_borrow)) if exceptional else 0
    effective = min(total, after_borrow + burst)
    out = dict(base)
    out.update({
        "base_paced_max_calls": base_cap,
        "paced_max_calls": effective,
        "opportunity_borrow_active": bool(borrowed > 0),
        "opportunity_borrow_calls": borrowed,
        "day_session_active": day_active,
        "session_exceptional_burst_active": bool(burst > 0),
        "session_exceptional_burst_calls": burst,
        "opportunity_quality": round(q, 4),
        "opportunity_setup": round(setup, 4),
        "opportunity_heat": heat_value,
        "opportunity_heat_components": heat,
        "borrow_min_quality": float(borrow_min_quality),
        "borrow_min_setup": float(borrow_min_setup),
        "borrow_min_heat": float(borrow_min_heat),
        "exceptional_quality": float(exceptional_quality),
        "day_session_start_utc": int(day_session_start_utc),
        "day_session_end_utc": int(day_session_end_utc),
        "policy": "overnight budget preservation + London/NY exceptional access; daily call/token caps unchanged",
    })
    return out


def reserve_ai_call(
    kind: str,
    *,
    budget: int = 0,
    estimated_tokens: int = 4000,
    max_calls: int = 0,
    kind_budget: int = 0,
    kind_max_calls: int = 0,
    kind_paced_max_calls: int = 0,
    kind_pacing_next_unlock: str = "",
    cooldown_key: str = "",
    cooldown_seconds: int = 0,
    signature: str = "",
    ignore_cooldown: bool = False,
) -> tuple[bool, str]:
    """Reserve one automatic AI call before it starts.

    v4.6.1 adds a provider circuit breaker and optional per-kind ceilings while retaining
    evidence-signature dedupe. A billing/quota outage never burns repeated retries/calls.
    """
    if runtime_stop_requested():
        return False, "runtime/manual STOP active"
    provider_ok, provider_reason = provider_reservation_allowed()
    if not provider_ok:
        return False, provider_reason

    budget = int(budget or 0)
    estimated_tokens = max(0, int(estimated_tokens or 0))
    max_calls = int(max_calls or 0)
    kind_budget = int(kind_budget or 0)
    kind_max_calls = int(kind_max_calls or 0)
    kind_paced_max_calls = int(kind_paced_max_calls or 0)
    used = budgeted_trading_tokens_today()
    calls = budgeted_ai_calls_today()
    used_kind = budgeted_trading_tokens_today(kind)
    calls_kind = budgeted_ai_calls_today(kind)

    if budget > 0:
        if used >= budget:
            return False, f"daily token budget reached ({used:,}/{budget:,})"
        if estimated_tokens and used + estimated_tokens > budget:
            return False, f"insufficient token budget reserve ({used:,}+~{estimated_tokens:,}>{budget:,})"
    if max_calls > 0 and calls >= max_calls:
        return False, f"daily AI call cap reached ({calls}/{max_calls})"
    if kind_budget > 0:
        if used_kind >= kind_budget:
            return False, f"{kind} token budget reached ({used_kind:,}/{kind_budget:,})"
        if estimated_tokens and used_kind + estimated_tokens > kind_budget:
            return False, f"{kind} token reserve would exceed budget ({used_kind:,}+~{estimated_tokens:,}>{kind_budget:,})"
    if kind_max_calls > 0 and calls_kind >= kind_max_calls:
        return False, f"{kind} daily call cap reached ({calls_kind}/{kind_max_calls})"
    if kind_paced_max_calls > 0 and calls_kind >= kind_paced_max_calls:
        suffix = f" until {kind_pacing_next_unlock}" if kind_pacing_next_unlock else ""
        return False, f"{kind} pacing cap reached ({calls_kind}/{kind_paced_max_calls}){suffix}"

    key = (cooldown_key or kind or "global")[:180]
    now = time.time()
    with _connect() as conn:
        row = conn.execute("SELECT last_ts, signature, calls FROM ai_gate WHERE key=?", (key,)).fetchone()
        if row and not ignore_cooldown:
            last_ts = float(row["last_ts"] or 0)
            previous_signature = str(row["signature"] or "")
            if signature and previous_signature == signature and now - last_ts < max(60, int(cooldown_seconds or 0)):
                return False, "same evidence already analyzed during anti-loop cooldown"
            if not signature and cooldown_seconds and now - last_ts < min(60, int(cooldown_seconds)):
                return False, f"AI rapid-repeat guard active for {int(min(60, int(cooldown_seconds)) - (now - last_ts))}s"
        prior_calls = int(row["calls"] or 0) if row else 0
        conn.execute(
            "INSERT INTO ai_gate(key,last_ts,signature,calls) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET last_ts=excluded.last_ts, signature=excluded.signature, calls=excluded.calls",
            (key, now, str(signature or "")[:300], prior_calls + 1),
        )
    return True, "reserved"


def ai_budget_status(*, budget: int = 0, max_calls: int = 0) -> dict[str, Any]:
    used = trading_tokens_today()
    calls = trading_ai_calls_today()
    budget = int(budget or 0)
    max_calls = int(max_calls or 0)
    budget_unlimited = budget <= 0
    calls_unlimited = max_calls <= 0
    provider = provider_guard_status()
    cooling = (
        (not budget_unlimited and used >= budget)
        or (not calls_unlimited and calls >= max_calls)
        or bool(provider.get("paused"))
    )
    return {
        "used_tokens": used,
        "budget_tokens": budget,
        "remaining_tokens": None if budget_unlimited else max(0, budget - used),
        "used_pct": None if budget_unlimited else round(used / max(1, budget) * 100.0, 2),
        "calls": calls,
        "max_calls": max_calls,
        "unlimited_tokens": budget_unlimited,
        "unlimited_calls": calls_unlimited,
        "anti_loop_active": True,
        "cooling": cooling,
        "provider": provider,
        "by_kind": usage_by_kind_today(),
        "budget_epoch": budget_epoch_status(),
        "budgeted_by_kind": budgeted_usage_by_kind_today(),
        "budgeted_used_tokens": budgeted_trading_tokens_today(),
        "budgeted_calls": budgeted_ai_calls_today(),
    }
