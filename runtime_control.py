from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import DATA_DIR

MANUAL_STOP_FILE: Path = DATA_DIR / "stan_manual_stop.json"


class RuntimeStoppedError(RuntimeError):
    """Raised when autonomous Stan work is attempted after STOP."""


_LOCK = threading.RLock()
_STOP_EVENT = threading.Event()
_RUNTIME_GENERATION_ID = ""
_RUNTIME_STATE = "STOPPED"
_RUNTIME_STARTED_AT = ""
_STOP_REQUESTED_AT = ""
_STOP_REASON = ""
_ACTIVE_AI_REQUESTS = 0
_AI_REQUESTS_STARTED = 0
_AI_REQUESTS_COMPLETED = 0
_AI_REQUESTS_CANCELLED = 0
_LAST_AI_KIND = ""
_LAST_AI_STARTED_AT = ""
_LAST_AI_FINISHED_AT = ""
_LAST_AI_ERROR = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def manual_stop_active() -> bool:
    return MANUAL_STOP_FILE.exists()


def manual_stop_info() -> dict[str, Any]:
    if not MANUAL_STOP_FILE.exists():
        return {"active": False}
    try:
        data = json.loads(MANUAL_STOP_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"active": True, **data}
    except Exception:
        pass
    return {"active": True, "reason": "manual stop"}


def begin_runtime_generation() -> str:
    """Start a fresh in-process autonomous runtime generation.

    The persistent manual-stop file remains authoritative across processes. This function
    never clears it; only the explicit Desktop START path may do that.
    """
    global _RUNTIME_GENERATION_ID, _RUNTIME_STATE, _RUNTIME_STARTED_AT, _STOP_REQUESTED_AT, _STOP_REASON
    if manual_stop_active():
        raise RuntimeStoppedError("Persistent manual STOP is active")
    with _LOCK:
        _STOP_EVENT.clear()
        _RUNTIME_GENERATION_ID = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        _RUNTIME_STATE = "RUNNING"
        _RUNTIME_STARTED_AT = _now()
        _STOP_REQUESTED_AT = ""
        _STOP_REASON = ""
        return _RUNTIME_GENERATION_ID


def request_runtime_stop(reason: str = "User pressed STOP") -> None:
    global _RUNTIME_STATE, _STOP_REQUESTED_AT, _STOP_REASON
    with _LOCK:
        _STOP_EVENT.set()
        _RUNTIME_STATE = "STOPPING"
        _STOP_REQUESTED_AT = _STOP_REQUESTED_AT or _now()
        _STOP_REASON = str(reason or "User pressed STOP")[:500]


def mark_runtime_stopped() -> None:
    global _RUNTIME_STATE
    with _LOCK:
        _STOP_EVENT.set()
        _RUNTIME_STATE = "STOPPED"


def runtime_stop_requested() -> bool:
    return _STOP_EVENT.is_set() or manual_stop_active()


def assert_runtime_active_for_ai(kind: str = "trading") -> None:
    """Hard gate used immediately before every autonomous model request."""
    if runtime_stop_requested():
        raise RuntimeStoppedError(f"Stan runtime stopped; model request blocked ({kind})")


def ai_request_started(kind: str) -> None:
    global _ACTIVE_AI_REQUESTS, _AI_REQUESTS_STARTED, _LAST_AI_KIND, _LAST_AI_STARTED_AT, _LAST_AI_ERROR
    assert_runtime_active_for_ai(kind)
    with _LOCK:
        _ACTIVE_AI_REQUESTS += 1
        _AI_REQUESTS_STARTED += 1
        _LAST_AI_KIND = str(kind or "model")[:160]
        _LAST_AI_STARTED_AT = _now()
        _LAST_AI_ERROR = ""


def ai_request_finished(*, cancelled: bool = False, error: str = "") -> None:
    global _ACTIVE_AI_REQUESTS, _AI_REQUESTS_COMPLETED, _AI_REQUESTS_CANCELLED, _LAST_AI_FINISHED_AT, _LAST_AI_ERROR
    with _LOCK:
        _ACTIVE_AI_REQUESTS = max(0, _ACTIVE_AI_REQUESTS - 1)
        if cancelled:
            _AI_REQUESTS_CANCELLED += 1
        else:
            _AI_REQUESTS_COMPLETED += 1
        _LAST_AI_FINISHED_AT = _now()
        _LAST_AI_ERROR = str(error or "")[:1200]


def interruptible_wait(seconds: float, *, step: float = 0.10) -> bool:
    """Wait while remaining immediately responsive to STOP. Returns False when stopped."""
    deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
    while True:
        if runtime_stop_requested():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        _STOP_EVENT.wait(min(max(0.02, step), remaining))


def runtime_snapshot() -> dict[str, Any]:
    with _LOCK:
        state = {
            "generation_id": _RUNTIME_GENERATION_ID,
            "state": _RUNTIME_STATE,
            "started_at": _RUNTIME_STARTED_AT,
            "stop_requested_at": _STOP_REQUESTED_AT,
            "stop_reason": _STOP_REASON,
            "stop_event": _STOP_EVENT.is_set(),
            "manual_stop": manual_stop_active(),
            "ai": {
                "in_flight": _ACTIVE_AI_REQUESTS,
                "requests_started": _AI_REQUESTS_STARTED,
                "requests_completed": _AI_REQUESTS_COMPLETED,
                "requests_cancelled": _AI_REQUESTS_CANCELLED,
                "last_kind": _LAST_AI_KIND,
                "last_started_at": _LAST_AI_STARTED_AT,
                "last_finished_at": _LAST_AI_FINISHED_AT,
                "last_error": _LAST_AI_ERROR,
            },
        }
    return state


def request_manual_stop(reason: str = "User pressed STOP") -> dict[str, Any]:
    request_runtime_stop(reason)
    MANUAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": str(reason or "User pressed STOP")[:500],
        "stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    tmp = Path(str(MANUAL_STOP_FILE) + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(MANUAL_STOP_FILE)
    return {"active": True, **payload}


def clear_manual_stop() -> None:
    try:
        MANUAL_STOP_FILE.unlink(missing_ok=True)
    except TypeError:  # Python < 3.8 compatibility fallback; harmless on supported versions.
        if MANUAL_STOP_FILE.exists():
            MANUAL_STOP_FILE.unlink()
