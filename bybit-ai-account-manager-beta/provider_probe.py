from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings

from journal import log_event
from resilience import AIProviderPausedError, ProviderCreditExhaustedError, run_sync_resilient
from runtime_control import RuntimeStoppedError, runtime_stop_requested
from trading_usage import provider_guard_status, record_trading_tokens, request_provider_probe, trip_provider_guard


PROBE_MODEL = "gpt-5.4-mini"


def _usage_summary(result: Any) -> dict[str, int]:
    try:
        usage = result.context_wrapper.usage
        return {
            "requests": int(getattr(usage, "requests", 0) or 0),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }
    except Exception:
        return {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}



def run_provider_probe(*, force: bool = False) -> dict[str, Any]:
    """Perform one tiny provider-health call that is independent of market thresholds/budgets.

    The provider guard itself atomically admits at most one recovery probe. A successful
    call closes the circuit inside resilience.run_sync_resilient/provider_call_succeeded.
    Billing exhaustion immediately re-trips the circuit without an autonomous retry loop.
    """
    if runtime_stop_requested():
        return {"ok": False, "recovered": False, "reason": "manual_stop", "provider": provider_guard_status()}
    if force:
        request_provider_probe()

    before = provider_guard_status()
    if not bool(before.get("paused")):
        return {"ok": True, "recovered": True, "reason": "provider already active", "provider": before, "tokens": 0}

    agent = Agent(
        name="Stan Provider Recovery Probe",
        model=PROBE_MODEL,
        instructions="Return exactly the word OK. Do not use tools and do not add explanation.",
        model_settings=ModelSettings(verbosity="low", max_tokens=16),
    )
    try:
        result = run_sync_resilient(
            agent,
            "OK",
            max_turns=1,
            kind="trading.provider_probe",
        )
        usage = _usage_summary(result)
        tokens = int(usage.get("total_tokens", 0) or 0)
        record_trading_tokens(tokens, kind="provider_probe")
        after = provider_guard_status()
        recovered = not bool(after.get("paused"))
        payload = {
            "ok": recovered,
            "recovered": recovered,
            "reason": "provider credit/access recovered" if recovered else "probe completed but provider circuit remains paused",
            "provider": after,
            "tokens": tokens,
            "model": PROBE_MODEL,
        }
        log_event("api.provider_probe", payload)
        return payload
    except RuntimeStoppedError:
        return {"ok": False, "recovered": False, "reason": "manual_stop", "provider": provider_guard_status()}
    except (ProviderCreditExhaustedError, AIProviderPausedError) as exc:
        payload = {
            "ok": False,
            "recovered": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "provider": provider_guard_status(),
            "tokens": 0,
            "model": PROBE_MODEL,
        }
        log_event("api.provider_probe_failed", payload)
        return payload
    except Exception as exc:
        # A non-billing provider failure (for example temporary transport/auth/model access)
        # must not leave the guard stuck in PROBING. Re-open a slow retry window instead
        # of hammering the provider every scheduler tick.
        status = provider_guard_status()
        if str(status.get("state") or "").upper() == "PROBING":
            trip_provider_guard(
                code="provider_probe_failed",
                reason="OpenAI provider recovery probe failed",
                error=f"{type(exc).__name__}: {exc}",
                probe_after_seconds=900,
            )
        payload = {
            "ok": False,
            "recovered": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "provider": provider_guard_status(),
            "tokens": 0,
            "model": PROBE_MODEL,
        }
        log_event("api.provider_probe_failed", payload)
        return payload
