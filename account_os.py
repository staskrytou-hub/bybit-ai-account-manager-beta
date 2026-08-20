from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from account_os_store import get_state as os_get_state, record_event, set_state as os_set_state
from promotion_ai import refresh_promotions
from promotion_store import latest_promotion_scan
from research_bootstrap import run_professional_bootstrap
from research_store import latest_bootstrap, get_research_state, set_research_state
from trading_config import has_bybit_credentials, load_trading_settings, apply_safe_autopilot_profile, audit_autopilot_growth_profile
from credential_guard import validate_autopilot_key
from trading_engine import TRADING_CONTROLLER
from trading_usage import (
    trading_tokens_today, ai_budget_status, provider_guard_status, trip_provider_guard,
    ensure_budget_epoch_compatible, provider_reservation_allowed,
)
from provider_probe import run_provider_probe
from trading_store import get_state as trading_get_state, set_state as trading_set_state
from prelaunch import run_prelaunch_audit
from opportunity_manager import build_opportunity_plan, merge_official_events_into_plan
from opportunity_os import scan_opportunity_os
from strategy_governor import current_strategy_governor
from promotion_executor import execute_safe_promotion_actions
from promotion_lifecycle import reward_audit_snapshot
from browser_operator import browser_available, launch_authorization_browser
from runtime_control import (
    begin_runtime_generation, manual_stop_active, manual_stop_info, mark_runtime_stopped,
    request_manual_stop, request_runtime_stop, runtime_snapshot, runtime_stop_requested,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime | None:
    try: return datetime.fromisoformat(value)
    except Exception: return None


def _migrate_known_provider_outage_once() -> None:
    """Carry a v4.6.0 persisted quota failure into the v4.6.1 provider circuit once.

    This avoids paying for one more doomed provider request immediately after upgrade. The
    one-shot marker prevents an old Spot error from re-opening the circuit after a successful
    manual recovery probe.
    """
    marker = "provider_guard_migrated_v461"
    if trading_get_state(marker, "0") == "1":
        return
    try:
        opp = os_get_state("opportunity_os", {})
        spot = opp.get("spot_last_decision") if isinstance(opp, dict) else {}
        reason = str((spot or {}).get("reason") or "") if isinstance(spot, dict) else ""
        low = reason.lower()
        if any(x in low for x in ("credit_balance_exhausted", "insufficient_quota", "no credits remaining")):
            trip_provider_guard(
                code="credit_balance_exhausted",
                reason="OpenAI API credit/quota exhausted (migrated from v4.6.0 state)",
                error=reason,
                probe_after_seconds=900,
            )
    finally:
        trading_set_state(marker, "1")


class StanAccountOS:
    """Background orchestrator. Chat/UI is optional; this core owns continuous account work."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bootstrap_thread: threading.Thread | None = None
        self._promo_thread: threading.Thread | None = None
        self._browser_thread: threading.Thread | None = None
        self._opportunity_thread: threading.Thread | None = None
        self._provider_probe_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._autopilot_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "started_at": "",
            "heartbeat_at": "",
            "message": "Stan Core stopped",
            "bootstrap_running": False,
            "promotion_running": False,
            "browser_running": False,
            "opportunity_running": False,
            "last_error": "",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = dict(self._status)
        status["trading"] = TRADING_CONTROLLER.status()
        status["settings"] = load_trading_settings()
        status["bybit_credentials_configured"] = has_bybit_credentials()
        status["latest_research"] = latest_bootstrap()
        promo = latest_promotion_scan()
        status["latest_promotion_scan"] = {
            "scanned_at": promo.get("scanned_at", "") if promo else "",
            "campaign_count": len(promo.get("campaigns", [])) if promo else 0,
        }
        status["trading_tokens_today"] = trading_tokens_today()
        cfg_for_budget = status.get("settings") if isinstance(status.get("settings"), dict) else load_trading_settings()
        status["ai_budget"] = ai_budget_status(
            budget=int(cfg_for_budget.get("trading_token_budget_daily", 0)),
            max_calls=int(cfg_for_budget.get("ai_max_calls_daily", 0)),
        )
        status["prelaunch"] = os_get_state("prelaunch_report", {})
        life_summary = os_get_state("promotion_lifecycle_summary", {})
        life_counts = (life_summary or {}).get("counts", {}) if isinstance(life_summary, dict) else {}
        browser_saved = os_get_state("browser_operator_status", {})
        browser_actions = list((browser_saved or {}).get("actions") or []) if isinstance(browser_saved, dict) else []
        status["action_summary"] = {
            "browser_state": str((browser_saved or {}).get("state") or "") if isinstance(browser_saved, dict) else "",
            "last_browser_at": str((browser_saved or {}).get("updated_at") or "") if isinstance(browser_saved, dict) else "",
            "lifecycle_counts": dict(life_counts) if isinstance(life_counts, dict) else {},
            "last_actions": browser_actions[-6:],
            "promotion_action_ai_tokens_daily_cap": int(cfg_for_budget.get("promotion_action_tokens_daily", 0) or 0),
            "promotion_action_ai_calls_daily_cap": int(cfg_for_budget.get("promotion_action_calls_daily", 0) or 0),
            "browser_interval_hours": int(cfg_for_budget.get("browser_action_refresh_hours", 12) or 12),
            "browser_cycles_daily_max": int(cfg_for_budget.get("browser_action_max_cycles_daily", 2) or 2),
            "background_only": bool(cfg_for_budget.get("browser_background_only", True)),
            "auth_handoff_required": os_get_state("browser_auth_handoff_required", {}),
            "action_policy": "DOM allowlist + canonical lifecycle dedupe; zero paid AI; reward browser cycles are background-only and low-frequency",
        }
        status["opportunity_plan"] = os_get_state("opportunity_plan", {})
        status["reward_audit"] = reward_audit_snapshot(status["opportunity_plan"] if isinstance(status["opportunity_plan"], dict) else {})
        status["strategy_governor"] = current_strategy_governor()
        status["execution_safety_lock"] = trading_get_state("execution_safety_lock", "0") == "1"
        status["execution_safety_reason"] = trading_get_state("execution_safety_reason", "")
        status["manual_stop"] = manual_stop_info()
        saved_browser = os_get_state("browser_operator_status", {})
        try:
            live_browser = browser_available()
        except Exception as exc:
            live_browser = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        if isinstance(saved_browser, dict) and saved_browser:
            merged_browser = dict(saved_browser)
            # Browser discovery/CDP reachability is only availability metadata. Do not let it
            # overwrite the richer operator state (READY/STALE/AUTH_REQUIRED/RETRY_WAIT) that
            # was established by an actual Playwright page health/action cycle.
            merged_browser.update({
                k: v for k, v in live_browser.items()
                if k not in {"actions", "updated_at", "state", "browser_health"}
            })
            if live_browser.get("available") is False:
                merged_browser["state"] = "UNAVAILABLE"
            elif not merged_browser.get("state"):
                merged_browser["state"] = live_browser.get("state", "AVAILABLE")
            if "actions" not in merged_browser:
                merged_browser["actions"] = []
            status["browser_operator"] = merged_browser
        else:
            status["browser_operator"] = live_browser
        status["opportunity_os"] = os_get_state("opportunity_os", {})
        status["promotion_lifecycle"] = os_get_state("promotion_lifecycle_summary", {})
        status["runtime"] = runtime_snapshot()
        status["active_background_tasks"] = sum(
            1 for t in (self._thread, self._bootstrap_thread, self._promo_thread, self._browser_thread, self._opportunity_thread, self._provider_probe_thread)
            if t is not None and t.is_alive()
        )
        return status

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)
            snap = dict(self._status)
        os_set_state("core_status", snap)

    def start(self) -> bool:
        if manual_stop_active():
            self._update(running=False, message="STOPPED MANUALLY — press START STAN to run again")
            return False
        if self._thread and self._thread.is_alive():
            return False
        begin_runtime_generation()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="StanAccountOS", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        request_runtime_stop("Stan Core stopping")
        self._stop.set()
        TRADING_CONTROLLER.stop()
        self._update(message="Stan Core stopping...")

    def hard_stop(self) -> dict[str, Any]:
        """Persistent user kill-switch. Nothing autonomous may restart until explicit START STAN."""
        latch = request_manual_stop("User pressed STOP")
        self._stop.set()
        TRADING_CONTROLLER.stop()
        self._update(running=False, message="STOPPED MANUALLY", last_error="")
        record_event("core.manual_stop", "User activated persistent manual STOP", latch)
        return {"accepted": True, "manual_stop": latch, "message": "Stan stopped. Only START STAN can clear this stop."}


    def start_autopilot(self) -> dict[str, Any]:
        """Idempotent one-button startup; scheduler and UI cannot arm two loops concurrently."""
        with self._autopilot_lock:
            return self._start_autopilot_locked()

    def _start_autopilot_locked(self) -> dict[str, Any]:
        """Detect the saved Bybit key, apply safe defaults, research, then run 24/7."""
        if runtime_stop_requested():
            raise RuntimeError("Persistent manual STOP is active. Only the explicit Desktop START STAN action may clear it.")
        if bool(TRADING_CONTROLLER.status().get("running")):
            current_cfg = load_trading_settings()
            return {
                "accepted": True, "already_running": True, "mode": str(current_cfg.get("mode", "observer")),
                "prelaunch": os_get_state("prelaunch_report", {}),
                "message": "Stan Autopilot is already running; duplicate START was ignored.",
            }
        if not has_bybit_credentials():
            raise RuntimeError("Bybit API key + secret are not configured yet. Add them once in Trading Setup, then press Start Stan.")
        guard = validate_autopilot_key()
        mode = str(guard.get("autopilot_mode", "shadow"))
        env = str(guard.get("key_environment", "auto"))
        cfg = apply_safe_autopilot_profile(mode=mode, key_environment=env)
        _migrate_known_provider_outage_once()
        ensure_budget_epoch_compatible("v4.6.9", {"v4.6.8", "v4.6.7", "v4.6.6", "v4.6.5", "v4.6.4"})
        growth_audit = audit_autopilot_growth_profile(cfg)
        if not bool(growth_audit.get("passed")):
            raise RuntimeError("Adaptive Growth preflight failed; Stan will not arm live trading with an inconsistent risk profile.")
        prelaunch = run_prelaunch_audit()
        if mode == "autopilot_live" and not bool(prelaunch.get("ready")):
            raise RuntimeError("Pre-launch audit blocked live Autopilot: " + "; ".join(prelaunch.get("fatal_failures") or []))
        record_event("autopilot.prelaunch", "Pre-launch audit passed" if prelaunch.get("ready") else "Pre-launch audit completed with blocks", prelaunch)
        record_event("autopilot.arm", str(guard.get("message", "Autopilot armed")), {
            "mode": mode, "environment": guard.get("environment"), "read_only": guard.get("read_only"),
            "live_armed": guard.get("live_armed"), "unsafe_wallet_permissions": guard.get("unsafe_wallet_permissions"),
            "growth_preflight": growth_audit,
        })
        if mode == "autopilot_live" and not bool(guard.get("live_armed")):
            raise RuntimeError(str(guard.get("blocked_reason") or "Mainnet live key was not safely armed."))
        if mode == "autopilot_live":
            learning_identity = f"{env}:{str(guard.get('key_id',''))}"
            prior_identity = trading_get_state("autopilot_learning_identity", "")
            if prior_identity != learning_identity or not trading_get_state("autopilot_learning_started_ms", ""):
                trading_set_state("autopilot_learning_identity", learning_identity)
                trading_set_state("autopilot_learning_started_ms", str(int(time.time() * 1000)))
                record_event("autopilot.learning_epoch", "Started a fresh Stan live-learning epoch for this Bybit API identity")
        prior_env = get_research_state("autopilot_bootstrap_env", "")
        needs_fresh_bootstrap = prior_env != env or get_research_state("bootstrap_complete", "0") != "1"
        if needs_fresh_bootstrap:
            set_research_state("bootstrap_complete", "0")
            set_research_state("autopilot_bootstrap_env", env)
        if needs_fresh_bootstrap:
            self.run_bootstrap_async(force=True)
        self.refresh_promotions_async(force=False)
        self.refresh_opportunities_async(force_research=False)
        self.start_trading()
        self._update(message=f"Stan Autopilot running • {mode}")
        return {
            "accepted": True, "mode": mode, "live_armed": bool(guard.get("live_armed")),
            "message": guard.get("message"), "settings": cfg, "growth_preflight": growth_audit, "prelaunch": prelaunch,
            "note": "Trading remains analysis-only until the professional research bootstrap is complete; execution then unlocks automatically if all safety gates still pass.",
        }

    def prelaunch(self) -> dict[str, Any]:
        report = run_prelaunch_audit()
        record_event("autopilot.prelaunch.manual", "Manual pre-launch audit completed", report)
        return report

    def start_trading(self) -> None:
        if runtime_stop_requested():
            raise RuntimeError("Stan is manually stopped.")
        TRADING_CONTROLLER.start()
        record_event("trading.start", "Trading Core start requested")

    def stop_trading(self) -> None:
        TRADING_CONTROLLER.stop()
        record_event("trading.stop", "Trading Core stop requested")

    def analyze_now(self) -> dict[str, Any]:
        if runtime_stop_requested():
            raise RuntimeError("Stan is manually stopped.")
        provider = provider_guard_status()
        if bool(provider.get("paused")):
            # v4.6.3: Analyze Now while the billing/provider circuit is open is a dedicated
            # tiny health probe. It does not depend on the current market candidate, setup
            # threshold, rotation policy, or historical per-kind AI budget counters.
            self._update(message="AI provider recovery probe running")
            result = run_provider_probe(force=True)
            if bool(result.get("recovered")):
                self._update(last_error="", message="AI FUNNEL ACTIVE — API CREDIT RECOVERED")
                record_event("api.provider_recovered.manual", "Manual provider recovery probe succeeded", result)
            else:
                self._update(message="AI PAUSED — provider recovery probe did not recover access")
                record_event("api.provider_probe.manual_failed", "Manual provider recovery probe failed", result)
            return {"mode": "provider_probe", **result}
        result = TRADING_CONTROLLER.analyze_now()
        record_event("trading.analyze", "Manual analysis completed", {"mode": result.get("mode")})
        return result

    def run_provider_probe_async(self, *, force: bool = False) -> bool:
        if runtime_stop_requested():
            return False
        if self._provider_probe_thread and self._provider_probe_thread.is_alive():
            return False
        allowed, _reason = provider_reservation_allowed()
        if not force and not allowed:
            return False

        def worker() -> None:
            self._update(message="AI provider recovery probe running")
            result = run_provider_probe(force=force)
            if bool(result.get("recovered")):
                self._update(last_error="", message="AI FUNNEL ACTIVE — API CREDIT RECOVERED")
                record_event("api.provider_recovered.auto", "Automatic provider recovery probe succeeded", result)
            else:
                # Credit exhaustion re-schedules the next probe inside resilience. Other
                # failures are visible but do not create a retry storm.
                self._update(message="AI PAUSED — provider recovery probe waiting for next window")
                record_event("api.provider_probe.auto_failed", "Automatic provider recovery probe did not recover access", result)

        self._provider_probe_thread = threading.Thread(target=worker, name="StanProviderProbe", daemon=True)
        self._provider_probe_thread.start()
        return True

    def run_bootstrap_async(self, force: bool = False) -> bool:
        if runtime_stop_requested():
            return False
        provider = provider_guard_status()
        if bool(provider.get("paused")):
            self._update(message="AI PAUSED — research bootstrap deferred; deterministic monitoring remains active")
            return False
        if self._bootstrap_thread and self._bootstrap_thread.is_alive():
            return False
        if not force:
            last = latest_bootstrap()
            if last and str(last.get("status")) == "running":
                return False

        def worker() -> None:
            if runtime_stop_requested():
                return
            self._update(bootstrap_running=True, message="Professional research bootstrap running")
            record_event("research.start", "Professional First Setup started")
            try:
                report = run_professional_bootstrap()
                record_event("research.finish", "Professional First Setup completed", {"run_id": report.get("run_id")})
                self._update(last_error="", message="Research baseline updated")
            except Exception as exc:
                text=f"{type(exc).__name__}: {exc}"
                record_event("research.error", text)
                self._update(last_error=text, message="Research bootstrap failed")
            finally:
                self._update(bootstrap_running=False)

        self._bootstrap_thread = threading.Thread(target=worker, name="StanResearchBootstrap", daemon=True)
        self._bootstrap_thread.start()
        return True

    def refresh_promotions_async(self, force: bool = False) -> bool:
        if runtime_stop_requested():
            return False
        if self._promo_thread and self._promo_thread.is_alive():
            return False
        cfg=load_trading_settings()
        if not bool(cfg.get("promotion_intelligence_enabled", True)):
            return False
        if not force and not bool(cfg.get("promotion_auto_ai_refresh_enabled", False)):
            # Cached campaign intelligence and Browser Operator remain usable without a paid scan.
            return False
        provider = provider_guard_status()
        if bool(provider.get("paused")) and not force:
            self._update(message="AI PAUSED — paid promotion refresh deferred; Browser Operator may use cached plan")
            return False

        def worker() -> None:
            self._update(promotion_running=True, message="Official Bybit promotion scan running")
            record_event("promotions.start", "Promotion Intelligence scan started")
            try:
                result=refresh_promotions(region_hint=str(cfg.get("promotion_region_hint", "auto")), force=force)
                count=len(result.get("campaigns", [])) if isinstance(result, dict) else 0
                try:
                    equity = float((os_get_state("prelaunch_report", {}) or {}).get("equity_usdt", 0.0) or 0.0)
                except Exception:
                    equity = 0.0
                plan = build_opportunity_plan(result if isinstance(result, dict) else {}, equity_usdt=equity)
                record_event("promotions.finish", f"Promotion scan completed: {count} campaign(s)")
                self._update(last_error="", message="Promotion Intelligence updated")
                if not runtime_stop_requested():
                    self.run_promotion_actions_async(plan=plan, force=False)
            except Exception as exc:
                text=f"{type(exc).__name__}: {exc}"
                record_event("promotions.error", text)
                self._update(last_error=text, message="Promotion scan failed")
            finally:
                self._update(promotion_running=False)

        self._promo_thread=threading.Thread(target=worker,name="StanPromotionScan",daemon=True)
        self._promo_thread.start()
        return True

    def run_promotion_actions_async(self, plan: dict[str, Any] | None = None, force: bool = False) -> bool:
        if runtime_stop_requested():
            return False
        if self._browser_thread and self._browser_thread.is_alive():
            return False
        cfg = load_trading_settings()
        if not bool(cfg.get("browser_operator_enabled", True)):
            return False
        saved_status = os_get_state("browser_operator_status", {})
        try:
            current_availability = browser_available()
        except Exception:
            current_availability = {"available": False}

        # v4.6.7: reward checks are intentionally low-frequency and non-intrusive.
        # Default is two cycles/day (12h spacing), bounded to 1..4/day by settings.
        now = _utcnow()
        max_cycles_daily = int(cfg.get("browser_action_max_cycles_daily", 2) or 2)
        history = os_get_state("browser_operator_cycle_history_v467", [])
        if not isinstance(history, list):
            history = []
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        kept: list[str] = []
        for raw in history[-16:]:
            dt = _parse(str(raw))
            if dt and dt >= day_start:
                kept.append(dt.isoformat(timespec="seconds"))
        if not force and len(kept) >= max_cycles_daily:
            return False

        last_raw = os_get_state("browser_operator_last_at", "")
        browser_minutes = int(cfg.get("browser_action_refresh_minutes", int(cfg.get("browser_action_refresh_hours", 12)) * 60) or 720)
        if not force and isinstance(last_raw, str) and last_raw:
            last = _parse(last_raw)
            if last and (now - last) < timedelta(minutes=max(30, browser_minutes)):
                return False
        action_plan = plan if isinstance(plan, dict) else os_get_state("opportunity_plan", {})
        if not isinstance(action_plan, dict):
            return False

        # Reserve the cycle slot before the thread starts so scheduler races/restarts cannot
        # exceed the daily nuisance budget. This state is not AI/token accounting.
        kept.append(now.isoformat(timespec="seconds"))
        os_set_state("browser_operator_cycle_history_v467", kept[-8:])

        def worker() -> None:
            if runtime_stop_requested():
                return
            cycle_started_at = _utcnow().isoformat(timespec="seconds")
            prior_browser = os_get_state("browser_operator_status", {})
            # Never present a persisted error from the previous cycle as the current
            # browser state. Keep it as history while the new cycle gets a clean BUSY state.
            watchdog_seconds = int(cfg.get("browser_cycle_timeout_seconds", 420) or 420)
            watchdog_deadline_at = (_utcnow() + timedelta(seconds=watchdog_seconds)).isoformat(timespec="seconds")
            working_status = {
                "available": True,
                "state": "BUSY",
                "session_connected": bool(current_availability.get("session_connected")),
                "actions": [],
                "previous_actions": list((prior_browser or {}).get("actions") or [])[:8] if isinstance(prior_browser, dict) else [],
                "previous_state": str((prior_browser or {}).get("state") or "") if isinstance(prior_browser, dict) else "",
                "previous_updated_at": str((prior_browser or {}).get("updated_at") or "") if isinstance(prior_browser, dict) else "",
                "cycle_started_at": cycle_started_at,
                "watchdog_timeout_seconds": watchdog_seconds,
                "watchdog_deadline_at": watchdog_deadline_at,
                "updated_at": cycle_started_at,
                "browser_health": {"state": "BUSY", "last_error": "", "connection_mode": "cdp" if current_availability.get("session_connected") else ""},
            }
            os_set_state("browser_operator_status", working_status)
            self._update(browser_running=True, message="Bybit Promotion Operator checking safe web actions")
            record_event("promotion.browser.start", "Safe Bybit browser action cycle started", {"cycle_started_at": cycle_started_at})
            try:
                result = execute_safe_promotion_actions(action_plan)
                os_set_state("browser_operator_status", result)
                os_set_state("browser_operator_last_at", _utcnow().isoformat(timespec="seconds"))
                lifecycle = result.get("lifecycle") if isinstance(result, dict) and isinstance(result.get("lifecycle"), dict) else {}
                counts = lifecycle.get("counts") if isinstance(lifecycle.get("counts"), dict) else {}
                if int(counts.get("AUTH_REQUIRED", 0) or 0) > 0:
                    os_set_state("browser_auth_handoff_required", {
                        "required": True,
                        "at": _utcnow().isoformat(timespec="seconds"),
                        "reason": "Rewards browser session needs login/2FA. No visible browser was opened automatically.",
                    })
                os_set_state("promotion_reward_audit", reward_audit_snapshot(action_plan))
                self._update(last_error="", message="Promotion web-action cycle completed in background")
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
                record_event("promotion.browser.error", text)
                self._update(last_error=text, message="Promotion Browser Operator needs attention")
            finally:
                self._update(browser_running=False)

        self._browser_thread = threading.Thread(target=worker, name="StanPromotionBrowser", daemon=True)
        self._browser_thread.start()
        return True

    def authorize_bybit_browser(self) -> dict[str, Any]:
        """Explicit visible human-auth handoff for Rewards Hub. Safe to call from Desktop."""
        if runtime_stop_requested():
            return {"opened": False, "reason": "manual STOP active"}
        result = launch_authorization_browser("https://www.bybit.com/en/rewards_hub")
        os_set_state("browser_auth_handoff", {**result, "at": _utcnow().isoformat(timespec="seconds")})
        record_event("promotion.browser_auth", "Opened visible Bybit authorization browser" if result.get("opened") else "Could not open Bybit authorization browser", result)
        return result

    def refresh_opportunities_async(self, force_research: bool = False) -> bool:
        if runtime_stop_requested():
            return False
        if self._opportunity_thread and self._opportunity_thread.is_alive():
            return False
        cfg = load_trading_settings()
        if not bool(cfg.get("opportunity_os_enabled", True)):
            return False

        def worker() -> None:
            if runtime_stop_requested():
                return
            self._update(opportunity_running=True, message="Opportunity OS scanning Spot/events/Earn")
            record_event("opportunity.start", "Opportunity OS scan started")
            try:
                pre = os_get_state("prelaunch_report", {})
                live_ready = bool((pre or {}).get("ready")) and get_research_state("bootstrap_complete", "0") == "1"
                state = scan_opportunity_os(force_research=force_research, allow_live_spot=live_ready)
                # v4.6.4: fresh official campaign/quest/reward URLs are merged into the
                # browser inspection queue deterministically. No paid Promotion AI is needed.
                plan = merge_official_events_into_plan(os_get_state("opportunity_plan", {}), list(state.get("official_events") or []))
                record_event(
                    "opportunity.finish",
                    f"Opportunity OS updated: {len(state.get('spot_candidates') or [])} Spot candidate(s), {len(state.get('official_events') or [])} official event(s)",
                    {"permission_gaps": state.get("permission_gaps", []), "errors": state.get("errors", []), "official_browser_queue_added": plan.get("official_event_browser_queue_added", 0)},
                )
                if bool(cfg.get("browser_operator_enabled", True)) and not runtime_stop_requested():
                    self.run_promotion_actions_async(plan=plan, force=False)
                self._update(last_error="", message="Opportunity OS updated")
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
                record_event("opportunity.error", text)
                self._update(last_error=text, message="Opportunity OS scan failed")
            finally:
                self._update(opportunity_running=False)

        self._opportunity_thread = threading.Thread(target=worker, name="StanOpportunityOS", daemon=True)
        self._opportunity_thread.start()
        return True

    def _scheduled_work(self) -> None:
        if runtime_stop_requested():
            return
        cfg=load_trading_settings()

        # v4.6.3: provider recovery is its own scheduler lane. A due probe does not wait
        # for a strong market candidate and cannot be blocked by Futures/Spot daily caps.
        provider = provider_guard_status()
        if bool(provider.get("paused")):
            allowed_probe, _probe_reason = provider_reservation_allowed()
            if allowed_probe:
                self.run_provider_probe_async(force=False)

        if bool(cfg.get("auto_start", False)) and has_bybit_credentials() and not bool(TRADING_CONTROLLER.status().get("running")):
            # Re-run key/risk/pre-launch validation on every Windows/Core restart instead of blindly resuming live execution.
            self.start_autopilot()
            return

        # First connection bootstrap: one high-value run, not continuous LLM polling.
        if has_bybit_credentials() and bool(cfg.get("auto_bootstrap_after_connection", True)):
            last=latest_bootstrap()
            if not last or str(last.get("status")) != "completed":
                self.run_bootstrap_async(force=False)

        # Research refresh on a slow cadence.
        last=latest_bootstrap()
        due=False
        if last and str(last.get("finished_at", "")):
            finished=_parse(str(last.get("finished_at")))
            if finished:
                due=(_utcnow()-finished)>=timedelta(hours=int(cfg.get("research_refresh_hours", 6)))
        if due and not self._bootstrap_thread_alive():
            self.run_bootstrap_async(force=True)

        promo=latest_promotion_scan()
        promo_due=True
        if promo:
            scanned=_parse(str(promo.get("scanned_at", "")))
            if scanned:
                promo_due=(_utcnow()-scanned)>=timedelta(hours=int(cfg.get("promotion_refresh_hours", 12)))
        if promo_due and bool(cfg.get("promotion_auto_ai_refresh_enabled", False)) and not bool(provider_guard_status().get("paused")):
            self.refresh_promotions_async(force=True)
        elif bool(cfg.get("browser_operator_enabled", True)):
            # Browser actions can continue from the cached opportunity plan without spending AI tokens.
            self.run_promotion_actions_async(force=False)

        # Unified Opportunity OS: Spot + official events + Alpha/Prediction discovery + Earn discovery.
        opp = os_get_state("opportunity_os", {})
        opp_due = True
        if isinstance(opp, dict) and str(opp.get("updated_at", "")):
            updated = _parse(str(opp.get("updated_at")))
            if updated:
                opp_due = (_utcnow() - updated) >= timedelta(minutes=int(cfg.get("opportunity_refresh_minutes", 15)))
        if opp_due:
            self.refresh_opportunities_async(force_research=False)

    def _bootstrap_thread_alive(self) -> bool:
        return bool(self._bootstrap_thread and self._bootstrap_thread.is_alive())

    def _loop(self) -> None:
        started=_utcnow().isoformat(timespec="seconds")
        self._update(running=True, started_at=started, heartbeat_at=started, message="Stan Core running")
        record_event("core.start", "Stan Account OS started")
        try:
            while not self._stop.is_set() and not runtime_stop_requested():
                try:
                    self._scheduled_work()
                    self._update(heartbeat_at=_utcnow().isoformat(timespec="seconds"), last_error="")
                except Exception as exc:
                    text=f"{type(exc).__name__}: {exc}"
                    record_event("core.error", text)
                    self._update(last_error=text, message="Stan Core scheduler error")
                self._stop.wait(15)
        finally:
            mark_runtime_stopped()
            self._update(running=False, heartbeat_at=_utcnow().isoformat(timespec="seconds"), message="Stan Core stopped")
            record_event("core.stop", "Stan Account OS stopped")


ACCOUNT_OS = StanAccountOS()
