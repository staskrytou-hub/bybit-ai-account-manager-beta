from __future__ import annotations

import re
import time
import queue
import threading
from collections.abc import Callable
from typing import Any

from agents import Runner

from journal import log_event
from settings import load_settings
from runtime_control import (
    RuntimeStoppedError, ai_request_finished, ai_request_started,
    assert_runtime_active_for_ai, interruptible_wait, runtime_stop_requested,
)
from trading_usage import begin_provider_call, provider_call_succeeded, trip_provider_guard

RetryHandler = Callable[[dict[str, object]], None]


class AIProviderPausedError(RuntimeError):
    """No provider request was sent because Stan's AI circuit breaker is open."""


class ProviderCreditExhaustedError(RuntimeError):
    """OpenAI/provider account has no usable API credit/quota; autonomous AI is paused."""


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:1800]


def is_credit_exhausted_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "credit_balance_exhausted", "insufficient_quota", "no credits remaining",
        "billing_hard_limit_reached", "billing hard limit", "quota has been exceeded",
    )
    return any(x in text for x in markers)


def is_provider_availability_error(exc: BaseException) -> bool:
    if isinstance(exc, (AIProviderPausedError, ProviderCreditExhaustedError)):
        return True
    return is_credit_exhausted_error(exc)


def _is_retryable(exc: BaseException) -> bool:
    # Billing/quota 429s are permanent until the account changes; retrying only wastes time
    # and can multiply requests once credit is restored. They trip the global provider circuit.
    if is_credit_exhausted_error(exc):
        return False
    status = _status_code(exc)
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    hints = (
        "rate limit", "ratelimit", "timeout", "timed out", "connection reset", "connection error",
        "temporarily unavailable", "service unavailable", "server error", "overloaded",
    )
    return any(h in name or h in text for h in hints)


def _header_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    for key in ("retry-after", "Retry-After"):
        try:
            value = headers.get(key)
            if value is not None:
                return max(0.0, float(value))
        except Exception:
            pass
    for key in ("retry-after-ms", "Retry-After-Ms"):
        try:
            value = headers.get(key)
            if value is not None:
                return max(0.0, float(value) / 1000.0)
        except Exception:
            pass
    return None


def _message_seconds(exc: BaseException) -> float | None:
    text = str(exc)
    patterns = [
        r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|sec|seconds?)",
        r"retry after\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|sec|seconds?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("m"):
            value /= 1000.0
        return max(0.0, value)
    return None


def _wait_seconds(exc: BaseException, attempt: int, max_wait: float) -> float:
    suggested = _header_seconds(exc) or _message_seconds(exc)
    if suggested is None:
        suggested = min(max_wait, 1.25 * (2 ** max(0, attempt - 1)))
    return min(max_wait, max(0.75, suggested + 0.35))


def run_sync_resilient(
    agent: Any,
    input_value: Any,
    *,
    session: Any | None = None,
    max_turns: int = 10,
    kind: str = "model",
    retry_handler: RetryHandler | None = None,
) -> Any:
    cfg = load_settings()
    retries = int(cfg.get("api_rate_limit_retries", 6))
    max_wait = float(cfg.get("api_rate_limit_max_wait_seconds", 45))
    governed = str(kind or "").startswith("trading.")
    if governed:
        retries = min(retries, 1)

    for attempt in range(retries + 1):
        if governed:
            assert_runtime_active_for_ai(kind)
            admitted, reason = begin_provider_call(kind)
            if not admitted:
                raise AIProviderPausedError(reason)
            ai_request_started(kind)
        error_text = ""
        cancelled = False
        try:
            value = Runner.run_sync(agent, input_value, session=session, max_turns=max_turns)
            if governed:
                provider_call_succeeded()
            return value
        except RuntimeStoppedError:
            cancelled = True
            raise
        except Exception as exc:
            error_text = _error_text(exc)
            if governed and runtime_stop_requested():
                cancelled = True
                raise RuntimeStoppedError(f"Stan runtime stopped during model request ({kind})") from exc
            if governed and is_credit_exhausted_error(exc):
                # One billing failure is enough. All autonomous AI paths pause globally and a
                # single recovery probe is allowed later (or immediately after user Analyze Now).
                try:
                    from trading_config import load_trading_settings
                    probe_minutes = int(load_trading_settings().get("ai_provider_probe_minutes", 15) or 15)
                except Exception:
                    probe_minutes = 15
                trip_provider_guard(
                    code="credit_balance_exhausted",
                    reason="OpenAI API credit/quota exhausted",
                    error=error_text,
                    probe_after_seconds=max(60, probe_minutes * 60),
                )
                log_event("api.provider_paused", {"kind": kind, "code": "credit_balance_exhausted", "error": error_text})
                raise ProviderCreditExhaustedError(error_text) from exc
            if not _is_retryable(exc) or attempt >= retries:
                raise
            wait = _wait_seconds(exc, attempt + 1, max_wait)
            event = {
                "kind": kind,
                "attempt": attempt + 1,
                "max_retries": retries,
                "wait_seconds": round(wait, 2),
                "status_code": _status_code(exc),
                "error": error_text,
            }
            log_event("api.retry", event)
            if retry_handler:
                try:
                    retry_handler(event)
                except Exception:
                    pass
            if governed:
                if not interruptible_wait(wait):
                    cancelled = True
                    raise RuntimeStoppedError(f"Stan runtime stopped during retry backoff ({kind})")
            else:
                time.sleep(wait)
        finally:
            if governed:
                ai_request_finished(cancelled=cancelled, error=error_text)

    raise RuntimeError("Retry loop ended unexpectedly")


def run_sync_resilient_isolated(
    agent: Any,
    input_value: Any,
    *,
    session: Any | None = None,
    max_turns: int = 10,
    kind: str = "model",
    retry_handler: RetryHandler | None = None,
) -> Any:
    """Run the synchronous Agents SDK runner on a clean daemon thread."""
    governed = str(kind or "").startswith("trading.")
    if governed:
        assert_runtime_active_for_ai(kind)

    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            value = run_sync_resilient(
                agent, input_value, session=session, max_turns=max_turns,
                kind=kind, retry_handler=retry_handler,
            )
            result_queue.put(("ok", value))
        except BaseException as exc:
            try:
                result_queue.put(("error", exc))
            except Exception:
                pass

    thread = threading.Thread(
        target=worker,
        name=f"StanAgentIsolated-{str(kind or 'model')[:48]}",
        daemon=True,
    )
    thread.start()

    while True:
        try:
            status, payload = result_queue.get(timeout=0.10)
        except queue.Empty:
            if governed and runtime_stop_requested():
                raise RuntimeStoppedError(f"Stan runtime stopped while waiting for isolated model request ({kind})")
            continue
        if status == "ok":
            return payload
        raise payload
