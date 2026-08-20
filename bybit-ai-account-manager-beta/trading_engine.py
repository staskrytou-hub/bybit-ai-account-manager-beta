from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from bybit_client import BybitClient, BybitAPIError
from journal import log_event
from market_analysis import build_market_snapshot
from research_store import research_context_for_symbol
from promotion_store import latest_promotion_scan, promotion_context_for_symbol, promotion_symbol_boosts
from risk_engine import evaluate_trade_candidate
from live_learning import live_learning_snapshot
from credential_guard import validate_autopilot_key
from trading_ai import analyze_snapshot
from trading_config import has_bybit_credentials, load_trading_settings
from trading_store import (
    get_state,
    set_state,
    record_cycle,
    record_assessment,
    get_paper_position,
    open_paper_position,
    update_paper_position,
    recent_assessments,
    recent_paper_trades,
)
from trading_usage import (
    record_trading_tokens, trading_tokens_today, reserve_ai_call, paced_daily_call_cap,
    opportunity_aware_paced_call_cap, session_opportunity_aware_paced_call_cap,
    release_ai_reservation, request_provider_probe, provider_guard_status,
)
from universe_scanner import select_active_symbol
from execution_verifier import confirm_market_entry, reconcile_execution_lock
from execution_eligibility import (
    bootstrap_from_recent_assessments, execution_restrictions, instrument_restriction_family,
    mark_family_restriction, mark_symbol_restriction, symbol_or_family_restriction, symbol_restriction,
)
from runtime_control import RuntimeStoppedError, manual_stop_active, runtime_stop_requested
from resilience import is_provider_availability_error
from strategy_governor import strategy_support
from adaptive_strategy_lab import live_adaptive_matches
from portfolio_learning import portfolio_state
from account_capacity import (
    unified_margin_state, live_position_inventory, futures_minimum_notional,
    instrument_leverage_cap, pre_ai_capacity_gate, recent_capacity_reject_gate,
)
from trade_proposal import (
    build_futures_proposal, veto_blocks_proposal, record_proposal_veto, clear_proposal_veto,
    record_proposal_approval, reusable_proposal_approval, clear_proposal_approval,
    bump_proposal_stat, proposal_stats, choose_best_preflight_candidate,
)

StatusCallback = Callable[[dict[str, Any]], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    try: return datetime.fromisoformat(value)
    except Exception: return None


def _wallet_equity(client: BybitClient) -> float | None:
    try:
        data = client.get_wallet_balance("USDT")
        items = data.get("list") or []
        if not items: return None
        first = items[0]
        for key in ("totalEquity", "totalWalletBalance"):
            try:
                value = float(first.get(key) or 0)
                if value > 0: return value
            except Exception: pass
    except Exception:
        return None
    return None


def _open_testnet_positions(client: BybitClient, symbol: str) -> list[dict[str, Any]]:
    try:
        rows = client.get_positions(symbol=symbol)
        return [p for p in rows if float(p.get("size") or 0) > 0]
    except Exception:
        return []



def _open_positions_all(client: BybitClient) -> list[dict[str, Any]]:
    try:
        rows = client.get_positions(settle_coin="USDT")
        return [p for p in rows if float(p.get("size") or 0) > 0]
    except Exception:
        return []


def _adaptive_market_slippage_pct(snapshot: dict[str, Any], cfg: dict[str, Any]) -> float:
    """Choose a bounded market-order slippage tolerance from live volatility/liquidity.

    The tolerance is an execution envelope, not extra loss-at-stop risk. It stays deliberately
    capped and is recorded in last_execution so an accepted-but-unfilled order can be audited.
    """
    try:
        base = float(cfg.get("live_order_slippage_pct", 0.25) or 0.25)
    except Exception:
        base = 0.25
    try:
        cap = float(cfg.get("live_order_slippage_cap_pct", 0.75) or 0.75)
    except Exception:
        cap = 0.75
    try:
        atr_pct = max(0.0, float(snapshot.get("atr_pct", 0.0) or 0.0))
    except Exception:
        atr_pct = 0.0
    try:
        spread_pct = max(0.0, float(snapshot.get("spread_bps", 0.0) or 0.0) / 100.0)
    except Exception:
        spread_pct = 0.0
    try:
        atr_factor = float(cfg.get("live_order_slippage_atr_factor", 0.15) or 0.15)
    except Exception:
        atr_factor = 0.15
    value = max(base, atr_pct * atr_factor, spread_pct * 2.0)
    return round(max(0.01, min(cap, value)), 2)


def _submit_confirm_market_entry(
    client: BybitClient, *, mode: str, symbol: str, side: str, risk: dict[str, Any],
    snapshot: dict[str, Any], cfg: dict[str, Any], order_prefix: str,
) -> dict[str, Any]:
    """Send one uniquely-linked market order and reconcile it without unsafe blind retries."""
    warnings: list[str] = []
    try:
        client.switch_position_mode(symbol=symbol, mode=0)
    except Exception as exc:
        warnings.append(f"position mode setup: {type(exc).__name__}: {exc}")
    try:
        client.set_leverage(symbol, float(risk["leverage"]))
    except Exception as exc:
        # Existing leverage may already be usable. The create-order response remains authoritative.
        warnings.append(f"leverage setup: {type(exc).__name__}: {exc}")

    order_link_id = f"{order_prefix}{int(time.time() * 1000)}"[:36]
    slippage_pct = _adaptive_market_slippage_pct(snapshot, cfg)
    response: dict[str, Any] = {}
    submit_error = ""
    explicit_reject = False
    try:
        response = client.place_order(
            symbol=symbol, side=side, qty=str(risk["qty"]), order_type="Market",
            stop_loss=str(risk["stop_loss"]), take_profit=str(risk["take_profit"]),
            order_link_id=order_link_id, slippage_tolerance_pct=slippage_pct,
        )
    except Exception as exc:
        submit_error = f"{type(exc).__name__}: {exc}"
        low = submit_error.lower()
        # A non-duplicate retCode is an explicit exchange-side rejection: no blind retry and no
        # permanent ambiguity lock. Transport/HTTP failures are reconciled by orderLinkId because
        # the request could have reached the exchange even when the response was lost.
        explicit_reject = "retcode=" in low and "duplicate" not in low

    ack = (response.get("result", {}) or {}) if isinstance(response, dict) else {}
    if explicit_reject:
        restriction = mark_symbol_restriction(symbol, submit_error, source="live_order_reject")
        family_restriction = None
        capacity_after_reject: dict[str, Any] = {}
        if "retcode=110007" in low or "available balance is insufficient" in low or "ab not enough" in low:
            try:
                capacity_after_reject = unified_margin_state(client)
                set_state("last_capacity_reject", json.dumps({
                    "symbol": symbol, "order_link_id": order_link_id, "error": submit_error,
                    "risk_notional_usdt": risk.get("notional_usdt"), "risk_leverage": risk.get("leverage"),
                    "capacity": capacity_after_reject, "ts": time.time(),
                }, ensure_ascii=False)[:12000])
            except Exception as capacity_exc:
                warnings.append(f"capacity refresh after 110007: {type(capacity_exc).__name__}: {capacity_exc}")
        if restriction and str(restriction.get("class")) == "agreement_required":
            try:
                instrument = client.get_instrument(symbol)
                family = instrument_restriction_family(instrument)
                if family:
                    family_restriction = mark_family_restriction(
                        family, submit_error, source_symbol=symbol, source="live_order_reject",
                    )
            except Exception as family_exc:
                warnings.append(f"execution family eligibility lookup: {type(family_exc).__name__}: {family_exc}")
        confirmation = {
            "confirmed": False, "filled": False, "terminal": True,
            "lifecycle": "submit_rejected_before_ack", "order_status": "RejectedBeforeAck",
            "order_id": "", "order_link_id": order_link_id, "reason": submit_error,
            "execution_restriction": restriction or {},
            "execution_family_restriction": family_restriction or {},
            "account_capacity_after_reject": capacity_after_reject,
        }
    else:
        try:
            confirmation = confirm_market_entry(
                client, symbol=symbol, order_id=str(ack.get("orderId", "")),
                order_link_id=order_link_id, stop_loss=float(risk["stop_loss"]),
                take_profit=float(risk["take_profit"]),
            )
        except Exception as exc:
            # Reconciliation itself failed. Do not retry the order: lock entries and surface the
            # exact ambiguity instead of pretending the trade failed or succeeded.
            reason = f"Execution reconciliation failed for {order_link_id}: {type(exc).__name__}: {exc}"
            set_state("execution_safety_lock", "1")
            set_state("execution_safety_reason", reason)
            set_state("execution_uncertain_symbol", symbol)
            set_state("execution_uncertain_order_id", str(ack.get("orderId", "")))
            set_state("execution_uncertain_order_link_id", order_link_id)
            set_state("execution_uncertain_stop", str(risk["stop_loss"]))
            set_state("execution_uncertain_tp", str(risk["take_profit"]))
            confirmation = {
                "confirmed": False, "filled": False, "uncertain": True,
                "lifecycle": "reconciliation_exception", "reason": reason,
                "order_id": str(ack.get("orderId", "")), "order_link_id": order_link_id,
            }

    filled = bool(confirmation.get("confirmed")) and bool(confirmation.get("filled"))
    submitted = bool(ack.get("orderId") or ack.get("orderLinkId")) or filled
    return {
        "mode": mode,
        "submitted": submitted,
        "executed": filled,
        "confirmed": filled,
        "response": ack,
        "order_link_id": order_link_id,
        "confirmation": confirmation,
        "position_confirmed": bool(confirmation.get("position_open")),
        "slippage_tolerance_pct": slippage_pct,
        "setup_warnings": warnings,
        "submit_error": submit_error,
    }


class TradingController:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._callback: StatusCallback | None = None
        self._promo_refresh_thread: threading.Thread | None = None
        self._status: dict[str, Any] = {
            "running": False,
            "state": "idle",
            "message": "Trading Core idle",
            "last_snapshot": None,
            "last_assessment": None,
            "last_risk": None,
            "last_execution": None,
            "last_error": "",
            "last_cycle_at": "",
            "current_analysis_key": "",
            "last_completed_analysis_key": "",
            "last_failed_analysis_key": "",
            "duplicate_cycles_prevented": 0,
            "live_account_capacity": {},
            "live_position_inventory": {},
            "current_learning_state": {},
            "ai_entry_pacing": {},
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["trading_tokens_today"] = trading_tokens_today()
        result["settings"] = load_trading_settings()
        result["paper_position"] = get_paper_position(str(result["settings"].get("symbol", "BTCUSDT")))
        result["last_watchlist_scan"] = get_state("last_watchlist_scan", "")
        result["active_symbol_history"] = get_state("active_symbol_history", "[]")
        result["proposal_stats"] = {"futures": proposal_stats("futures"), "spot": proposal_stats("spot")}
        result["execution_restrictions"] = execution_restrictions()
        try:
            result["last_proposal"] = json.loads(get_state("last_futures_proposal", "{}") or "{}")
        except Exception:
            result["last_proposal"] = {}
        return result

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._status.update(changes)
            payload = dict(self._status)
        if self._callback:
            try: self._callback(payload)
            except Exception: pass

    def _stop_requested(self) -> bool:
        return self._stop.is_set() or runtime_stop_requested()

    def _abort_result(self, stage: str) -> dict[str, Any]:
        self._update(state="manual_stop", message=f"STOPPED MANUALLY during {stage}")
        log_event("trading.manual_stop", {"stage": stage})
        return {"ai_called": False, "mode": str(load_trading_settings().get("mode", "observer")), "stopped": True, "stage": stage}

    def start(self, callback: StatusCallback | None = None) -> bool:
        if manual_stop_active():
            self._update(running=False, state="manual_stop", message="Stan is manually stopped")
            return False
        if self._thread and self._thread.is_alive():
            if callback: self._callback = callback
            return False
        self._callback = callback
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="StanTradingCore", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._update(state="stopping", message="Stopping after current cycle...")

    def analyze_now(self) -> dict[str, Any]:
        if self._stop_requested():
            return self._abort_result("manual analysis")
        return self.run_cycle(force_ai=True)

    def _loop(self) -> None:
        self._update(running=True, state="running", message="Trading Core running")
        try:
            while not self._stop_requested():
                try:
                    self.run_cycle(force_ai=False)
                except Exception as exc:
                    text=f"{type(exc).__name__}: {exc}"
                    log_event("trading.cycle.error", {"error": text})
                    self._update(last_error=text, state="error", message=text)
                cfg=load_trading_settings()
                wait=max(5,int(cfg.get("poll_seconds",20)))
                self._stop.wait(wait)
        finally:
            self._update(running=False, state="idle", message="Trading Core stopped")

    def _maybe_refresh_promotions(self, cfg: dict[str, Any]) -> None:
        # v4.6.1: Account OS is the sole owner of promotion refresh cadence.
        # Trading cycles never start paid Promotion Intelligence in parallel.
        return

    def run_cycle(self, force_ai: bool = False) -> dict[str, Any]:
        # ThreadingHTTPServer can call Analyze Now while the background loop is already inside a
        # cycle. Serialize cycles so the same snapshot cannot trigger two concurrent model calls.
        if not self._cycle_lock.acquire(blocking=False):
            with self._lock:
                self._status["duplicate_cycles_prevented"] = int(self._status.get("duplicate_cycles_prevented", 0) or 0) + 1
            return {"ai_called": False, "busy": True, "reason": "analysis cycle already in progress", "mode": str(load_trading_settings().get("mode", "observer"))}
        try:
            return self._run_cycle_locked(force_ai=force_ai)
        finally:
            self._cycle_lock.release()

    def _run_cycle_locked(self, force_ai: bool = False) -> dict[str, Any]:
        if self._stop_requested():
            return self._abort_result("cycle start")
        cfg=load_trading_settings()
        # v4.6.5 Zero-Waste Eligibility: learn exchange-side contract blocks from persisted
        # execution history before scanning. This is local/no-token and prevents repeated AI
        # analysis for contracts Bybit has already said this account cannot trade.
        bootstrap_from_recent_assessments(limit=80)
        configured_symbol=str(cfg["symbol"]).upper()
        interval=str(cfg["interval"])
        mode=str(cfg["mode"])
        symbol=configured_symbol
        prebuilt_snapshot: dict[str, Any] | None = None

        # v4.6.6 account-capacity/restart reconciliation. Query Bybit BEFORE paid AI so
        # a position that survived STOP/build/restart (for example BTCUSDT) is immediately
        # adopted into portfolio state, never pyramided accidentally, and its consumed margin
        # is reflected in the capacity available to the next proposal. This is token-free.
        live_open_symbols: set[str] = set()
        live_state_client: BybitClient | None = None
        live_inventory: dict[str, Any] = {}
        live_margin_state: dict[str, Any] = {}
        live_margin_error = ""
        if mode == "autopilot_live" and has_bybit_credentials():
            try:
                live_state_client = BybitClient(testnet=False, authenticated=True)
                live_inventory = live_position_inventory(live_state_client)
                live_open_symbols = {str(x).upper() for x in list(live_inventory.get("symbols") or []) if str(x)}
                if live_inventory.get("error"):
                    log_event("trading.pre_ai_position_inventory.error", {"error": live_inventory.get("error")})
            except Exception as live_state_exc:
                log_event("trading.pre_ai_live_state.error", {"error": f"{type(live_state_exc).__name__}: {live_state_exc}"})
            if live_state_client is not None:
                try:
                    live_margin_state = unified_margin_state(live_state_client)
                except Exception as margin_exc:
                    live_margin_error = f"{type(margin_exc).__name__}: {margin_exc}"
                    log_event("trading.pre_ai_account_capacity.error", {"error": live_margin_error})
            self._update(
                live_position_inventory=live_inventory,
                live_account_capacity={**live_margin_state, **({"error": live_margin_error} if live_margin_error else {})},
            )
            # v4.6.7: keep learning telemetry current even during long periods with no paid AI.
            # This is rate-limited to one Bybit closed-PnL reconciliation every 15 minutes.
            last_learning_raw = get_state("current_learning_state_updated_at", "")
            last_learning_at = _parse_iso(last_learning_raw) if last_learning_raw else None
            learning_due = last_learning_at is None or (_utcnow() - last_learning_at) >= timedelta(minutes=15)
            if learning_due:
                try:
                    learning_equity = float(live_margin_state.get("total_equity_usd", 0.0) or 0.0) if live_margin_state else 0.0
                    if learning_equity <= 0:
                        learning_equity = float(_wallet_equity(live_state_client) or 0.0)
                    if learning_equity > 0:
                        current_learning = live_learning_snapshot(live_state_client, learning_equity)
                        set_state("current_learning_state", json.dumps(current_learning, ensure_ascii=False)[:16000])
                        set_state("current_learning_state_updated_at", _utcnow().isoformat(timespec="seconds"))
                        self._update(current_learning_state=current_learning)
                except Exception as learning_exc:
                    log_event("trading.current_learning.reconcile_error", {"error": f"{type(learning_exc).__name__}: {learning_exc}"})
            else:
                try:
                    cached_learning = json.loads(get_state("current_learning_state", "{}") or "{}")
                    if isinstance(cached_learning, dict) and cached_learning:
                        self._update(current_learning_state=cached_learning)
                except Exception:
                    pass

        capacity_enabled = bool(cfg.get("futures_capacity_pre_ai_enabled", True)) and mode == "autopilot_live"
        capacity_available = float(live_margin_state.get("total_available_balance_usd", 0.0) or 0.0) if live_margin_state else 0.0
        capacity_util_pct = float(cfg.get("futures_available_balance_utilization_pct", 82.0) or 82.0)
        capacity_reserve = float(cfg.get("futures_available_balance_reserve_usdt", 2.0) or 0.0)
        try:
            capacity_reject_record = json.loads(get_state("last_capacity_reject", "{}") or "{}")
            if not isinstance(capacity_reject_record, dict):
                capacity_reject_record = {}
        except Exception:
            capacity_reject_record = {}
        capacity_reject_cooldown = float(cfg.get("futures_capacity_reject_cooldown_minutes", 20) or 20)
        capacity_reject_recovery = float(cfg.get("futures_capacity_reject_recovery_usdt", 3.0) or 3.0)
        global_pre_ai_block = ""
        if mode == "autopilot_live" and bool(cfg.get("portfolio_block_unprotected_positions", True)) and list(live_inventory.get("unprotected") or []):
            global_pre_ai_block = "live Futures position lacks verified exchange-side SL/TP protection: " + ", ".join(list(live_inventory.get("unprotected") or [])[:4])
        if (
            not global_pre_ai_block and mode == "autopilot_live" and live_open_symbols
            and bool(cfg.get("futures_require_capacity_state_with_open_positions", True)) and not live_margin_state
        ):
            global_pre_ai_block = "cannot verify current Bybit available balance while an existing Futures position is consuming margin"

        if bool(cfg.get("auto_symbol_selection", True)):
            last_scan=_parse_iso(get_state("last_symbol_scan", ""))
            scan_due=last_scan is None or (_utcnow()-last_scan) >= timedelta(minutes=int(cfg.get("symbol_scan_minutes",30)))
            if scan_due:
                try:
                    promo_boosts = promotion_symbol_boosts() if bool(cfg.get("promotion_intelligence_enabled", True)) else {}
                    scanner_cap = float(cfg.get("max_notional_usdt", 50.0))
                    if mode == "autopilot_live" and has_bybit_credentials():
                        try:
                            scan_equity = _wallet_equity(BybitClient(testnet=False, authenticated=True)) or 0.0
                            scanner_cap = max(scanner_cap, scan_equity * float(cfg.get("growth_mature_exposure_pct", 180.0)) / 100.0)
                        except Exception:
                            pass
                    try:
                        recent_symbols = json.loads(get_state("active_symbol_history", "[]") or "[]")
                        if not isinstance(recent_symbols, list): recent_symbols = []
                    except Exception:
                        recent_symbols = []
                    selection=select_active_symbol(
                        watchlist_size=int(cfg.get("live_watchlist_size",8)),
                        decision_interval=interval,
                        testnet=False,
                        promotion_boosts=promo_boosts,
                        promotion_weight=float(cfg.get("promotion_trade_alignment_weight", 0.05)),
                        promotion_min_base_setup=float(cfg.get("promotion_min_base_setup", 0.45)),
                        max_safe_notional_usdt=scanner_cap,
                        recent_symbols=[str(x) for x in recent_symbols],
                        rotation_margin=float(cfg.get("market_rotation_margin", 0.08)),
                        dominance_margin=float(cfg.get("market_dominance_margin", 0.12)),
                    )
                    selected=str(selection.get("selected_symbol", configured_symbol)).upper()

                    # v4.6.3: proposal-aware watchlist preflight. The scanner's top score is
                    # useful for regime discovery, but an extended/non-executable top market can
                    # otherwise monopolize the paid funnel. Build full LOCAL snapshots for only
                    # the top few scanner rows, then prefer the best executable proposal before AI.
                    preflight_rows: list[dict[str, Any]] = []
                    preflight_n = int(cfg.get("proposal_watchlist_preflight", 4) or 4)
                    usable_preflight = 0
                    # v4.6.5: blocked/infeasible contracts do not consume the four proposal
                    # preflight slots. Continue down the watchlist until Stan has evaluated up
                    # to four actually trade-eligible alternatives, so one TradFi agreement
                    # block cannot starve crypto opportunities behind it.
                    for candidate in list(selection.get("candidates") or []):
                        if usable_preflight >= preflight_n:
                            break
                        candidate_symbol = str(candidate.get("symbol", "")).upper()
                        if not candidate_symbol or candidate.get("infeasible_for_safe_cap"):
                            continue
                        if candidate_symbol in live_open_symbols:
                            preflight_rows.append({
                                "symbol": candidate_symbol,
                                "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                "proposal": {"eligible": False, "reason": "pre-AI block: live Futures position already open on symbol"},
                                "veto_blocked": False,
                            })
                            continue
                        candidate_instrument = {
                            "symbol": candidate_symbol,
                            "baseCoin": candidate.get("base_coin", ""),
                            "symbolType": candidate.get("symbol_type", ""),
                            "displayName": candidate.get("display_name", ""),
                            "contractType": candidate.get("contract_type", ""),
                        }
                        restriction = symbol_or_family_restriction(candidate_symbol, candidate_instrument)
                        if bool(restriction.get("blocked")):
                            preflight_rows.append({
                                "symbol": candidate_symbol,
                                "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                "proposal": {
                                    "eligible": False,
                                    "reason": f"pre-AI exchange eligibility block: {restriction.get('class','restricted')} — {restriction.get('reason','exchange rejected prior entry')}",
                                },
                                "veto_blocked": False,
                                "execution_restriction": restriction,
                            })
                            continue
                        if global_pre_ai_block:
                            usable_preflight += 1
                            preflight_rows.append({
                                "symbol": candidate_symbol,
                                "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                "proposal": {"eligible": False, "reason": f"pre-AI portfolio/capacity block: {global_pre_ai_block}"},
                                "veto_blocked": False,
                            })
                            continue
                        if capacity_enabled and live_margin_state:
                            min_notional = float(candidate.get("min_order_notional_estimate", 0.0) or 0.0)
                            capacity_gate = pre_ai_capacity_gate(
                                available_balance_usd=capacity_available, minimum_notional_usdt=min_notional,
                                leverage_cap=float(cfg.get("max_leverage", 4.0) or 4.0),
                                utilization_pct=capacity_util_pct, reserve_usdt=capacity_reserve,
                            )
                            if not bool(capacity_gate.get("allowed")):
                                preflight_rows.append({
                                    "symbol": candidate_symbol,
                                    "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                    "proposal": {"eligible": False, "reason": f"pre-AI account capacity block: {capacity_gate.get('reason')}"},
                                    "veto_blocked": False,
                                    "account_capacity_gate": capacity_gate,
                                })
                                continue
                            reject_gate = recent_capacity_reject_gate(
                                capacity_reject_record, symbol=candidate_symbol,
                                current_available_balance_usd=capacity_available, now_ts=time.time(),
                                cooldown_minutes=capacity_reject_cooldown, recovery_usdt=capacity_reject_recovery,
                            )
                            if bool(reject_gate.get("blocked")):
                                preflight_rows.append({
                                    "symbol": candidate_symbol,
                                    "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                    "proposal": {"eligible": False, "reason": f"pre-AI recent capacity-reject memory: {reject_gate.get('reason')}"},
                                    "veto_blocked": False,
                                    "account_capacity_reject_gate": reject_gate,
                                })
                                continue
                        usable_preflight += 1
                        try:
                            candidate_snapshot = build_market_snapshot(candidate_symbol, interval, testnet_market_data=bool(cfg.get("market_data_testnet",False)))
                            candidate_strategy = strategy_support(candidate_symbol, interval)
                            candidate_proposal = build_futures_proposal(candidate_snapshot, cfg, strategy_supported=bool(candidate_strategy.get("supported")))
                            candidate_veto = False
                            candidate_veto_reason = ""
                            if bool(candidate_proposal.get("eligible")):
                                candidate_veto, candidate_veto_reason = veto_blocks_proposal(candidate_proposal, interval, "futures")
                            preflight_rows.append({
                                "symbol": candidate_symbol,
                                "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                "snapshot": candidate_snapshot,
                                "proposal": candidate_proposal,
                                "veto_blocked": candidate_veto,
                                "veto_reason": candidate_veto_reason,
                            })
                        except Exception as preflight_exc:
                            preflight_rows.append({
                                "symbol": candidate_symbol,
                                "scanner_score": float(candidate.get("scanner_score", 0.0) or 0.0),
                                "proposal": {"eligible": False, "reason": f"preflight error: {type(preflight_exc).__name__}"},
                                "veto_blocked": False,
                            })
                    proposal_best = choose_best_preflight_candidate(preflight_rows)
                    compact_preflight = []
                    for row in preflight_rows:
                        prop = row.get("proposal") or {}
                        compact_preflight.append({
                            "symbol": row.get("symbol"),
                            "scanner_score": round(float(row.get("scanner_score", 0.0) or 0.0), 4),
                            "eligible": bool(prop.get("eligible")),
                            "action": prop.get("action", ""),
                            "quality": prop.get("quality", 0.0),
                            "priority": prop.get("priority", ""),
                            "reason": row.get("veto_reason") if row.get("veto_blocked") else prop.get("reason", ""),
                        })
                    selection["proposal_preflight"] = {
                        "evaluated": len(preflight_rows),
                        "eligible": sum(1 for row in preflight_rows if bool((row.get("proposal") or {}).get("eligible")) and not row.get("veto_blocked")),
                        "scanner_selected_symbol": selected,
                        "rows": compact_preflight,
                    }
                    if proposal_best is not None:
                        selected = str(proposal_best.get("symbol", selected)).upper()
                        prebuilt_snapshot = proposal_best.get("snapshot") if isinstance(proposal_best.get("snapshot"), dict) else None
                        selection["selected_symbol"] = selected
                        selection["proposal_preflight"]["selected_symbol"] = selected
                        selection["proposal_preflight"]["selection_reason"] = "best executable deterministic proposal before paid AI"

                    recent_symbols = [str(x).upper() for x in recent_symbols if str(x).upper() != selected] + [selected]
                    set_state("active_symbol_history", json.dumps(recent_symbols[-8:]))
                    set_state("active_symbol",selected)
                    set_state("last_symbol_scan",_utcnow().isoformat())
                    set_state("last_watchlist_scan",json.dumps(selection,ensure_ascii=False)[:50000])
                    symbol=selected
                except Exception as scan_exc:
                    log_event("trading.symbol_scan.error", {"error": f"{type(scan_exc).__name__}: {scan_exc}"})
                    symbol=(get_state("active_symbol", configured_symbol) or configured_symbol).upper()
            else:
                symbol=(get_state("active_symbol", configured_symbol) or configured_symbol).upper()
        self._update(state="market_data", message=f"Reading {symbol} market data..." + (" (auto-selected)" if symbol != configured_symbol else ""))
        snapshot = prebuilt_snapshot if isinstance(prebuilt_snapshot, dict) and str(prebuilt_snapshot.get("symbol", "")).upper() == symbol else build_market_snapshot(symbol, interval, testnet_market_data=bool(cfg.get("market_data_testnet",False)))
        self._update(last_snapshot=snapshot, last_cycle_at=_utcnow().isoformat(timespec="seconds"), last_error="")
        if self._stop_requested():
            return self._abort_result("market data")

        if mode == "paper":
            closed=update_paper_position(symbol,float(snapshot["price"]))
            if closed and closed.get("closed_at"):
                log_event("trading.paper.close", closed)

        candle_key=f"last_ai_candle:{symbol}:{interval}"
        last_ai_candle=int(get_state(candle_key,"0") or 0)
        closed_candle=int(snapshot["closed_candle_start_ms"])
        analysis_key=f"futures:{symbol}:{interval}:{closed_candle}"
        last_completed_key = str(get_state("last_completed_analysis_key", "") or "")
        last_failed_key = str(get_state("last_failed_analysis_key", "") or "")
        new_candle=closed_candle > last_ai_candle
        heartbeat_key=f"heartbeat:{symbol}:{interval}"
        heartbeat=int(get_state(heartbeat_key,"0") or 0)+1 if new_candle else int(get_state(heartbeat_key,"0") or 0)
        if new_candle: set_state(heartbeat_key,str(heartbeat))
        setup=float(snapshot.get("setup_strength",0.0) or 0.0)
        budget=int(cfg.get("trading_token_budget_daily",0))
        governor_reason = "proposal prefilter"

        # v4.6.3 Action Engine: build a concrete, deterministic entry/SL/TP proposal BEFORE
        # spending model tokens. AI is now a safety verifier, not the primary signal generator.
        strategy_state = strategy_support(symbol, interval)
        proposal = build_futures_proposal(snapshot, cfg, strategy_supported=bool(strategy_state.get("supported")))
        set_state("last_futures_proposal", json.dumps(proposal, ensure_ascii=False)[:12000])
        veto_blocked = False
        veto_reason = ""
        if bool(proposal.get("eligible")):
            veto_blocked, veto_reason = veto_blocks_proposal(proposal, interval, "futures")

        # Deterministic execution impossibilities are rejected before a model call. These
        # checks remain separate from proposal quality so a future proposal builder cannot
        # accidentally weaken spread/staleness safety.
        hard_ai_block = global_pre_ai_block
        spread_bps = float(snapshot.get("spread_bps", 999.0) or 999.0)
        if not hard_ai_block and spread_bps > float(cfg.get("max_spread_bps", 12.0)):
            hard_ai_block = f"spread {spread_bps:.2f} bps exceeds execution max {float(cfg.get('max_spread_bps',12.0)):.2f}"
        captured_ms = int(snapshot.get("captured_at_ms", 0) or 0)
        if not hard_ai_block and captured_ms > 0:
            age_seconds = max(0.0, time.time() - captured_ms / 1000.0)
            if age_seconds > float(cfg.get("max_data_age_seconds", 90)):
                hard_ai_block = f"market snapshot stale ({age_seconds:.0f}s)"
        if not hard_ai_block and symbol in live_open_symbols:
            hard_ai_block = "live Futures position already open on this symbol; paid AI skipped before verification"
        selected_instrument: dict[str, Any] = {}
        try:
            # Public metadata lookup only; no model tokens. It also protects manual Analyze Now
            # from rediscovering an unsigned TradFi agreement on a sibling contract.
            selected_instrument = (live_state_client or BybitClient(testnet=False)).get_instrument(symbol)
        except Exception:
            selected_instrument = {}
        exchange_restriction = symbol_or_family_restriction(symbol, selected_instrument)
        if not hard_ai_block and bool(exchange_restriction.get("blocked")):
            hard_ai_block = (
                f"exchange eligibility block {exchange_restriction.get('class','restricted')}: "
                f"{exchange_restriction.get('reason','Bybit rejected prior entry')} "
                f"(retry after {exchange_restriction.get('blocked_until','later')})"
            )
        selected_capacity_gate: dict[str, Any] = {}
        if not hard_ai_block and capacity_enabled and live_margin_state:
            exact_min_notional = futures_minimum_notional(selected_instrument, float(snapshot.get("price", 0.0) or 0.0))
            exact_leverage_cap = instrument_leverage_cap(selected_instrument, float(cfg.get("max_leverage", 4.0) or 4.0))
            selected_capacity_gate = pre_ai_capacity_gate(
                available_balance_usd=capacity_available, minimum_notional_usdt=exact_min_notional,
                leverage_cap=exact_leverage_cap, utilization_pct=capacity_util_pct, reserve_usdt=capacity_reserve,
            )
            if not bool(selected_capacity_gate.get("allowed")):
                hard_ai_block = f"account capacity block: {selected_capacity_gate.get('reason')}"
            if not hard_ai_block:
                selected_reject_gate = recent_capacity_reject_gate(
                    capacity_reject_record, symbol=symbol, current_available_balance_usd=capacity_available,
                    now_ts=time.time(), cooldown_minutes=capacity_reject_cooldown,
                    recovery_usdt=capacity_reject_recovery,
                )
                if bool(selected_reject_gate.get("blocked")):
                    hard_ai_block = f"recent capacity-reject memory: {selected_reject_gate.get('reason')}"

        cached_approval = reusable_proposal_approval(proposal, interval, "futures") if bool(proposal.get("eligible")) else {}
        use_cached_approval = bool(cached_approval) and new_candle and not veto_blocked and not hard_ai_block and not force_ai

        # Automatic paid AI now requires a NEW closed candle plus an executable proposal.
        # Manual Analyze Now remains a diagnostic escape hatch when provider access is active.
        should_ai = bool(cfg.get("ai_enabled", True)) and not hard_ai_block and (
            bool(force_ai) or (new_candle and bool(proposal.get("eligible")) and not veto_blocked)
        )
        # A still-valid APPROVE for the same structural signature can be reused without a paid call.
        should_ai = should_ai or use_cached_approval
        if not force_ai:
            if hard_ai_block:
                governor_reason = f"deterministic pre-AI block: {hard_ai_block}"
            elif veto_blocked:
                governor_reason = veto_reason
            elif not bool(proposal.get("eligible")):
                governor_reason = str(proposal.get("reason") or "no executable proposal")
            elif not new_candle:
                governor_reason = "waiting for new completed candle"

        if should_ai and analysis_key == last_completed_key:
            should_ai = False
            governor_reason = "exact snapshot/candle already completed"
        elif should_ai and analysis_key == last_failed_key and not force_ai:
            failed_at = _parse_iso(get_state("last_failed_analysis_at", ""))
            if failed_at and (_utcnow() - failed_at) < timedelta(minutes=5):
                should_ai = False
                governor_reason = "recent analysis failure cooldown; market remains under deterministic monitoring"

        pacing_enabled = bool(cfg.get("futures_entry_pacing_enabled", True)) and not bool(force_ai)
        pacing_window_hours = int(cfg.get("futures_entry_pacing_window_hours", 4) or 4)
        opportunity_enabled = bool(cfg.get("futures_opportunity_governor_enabled", True)) and not bool(force_ai)
        proposal_quality = float(proposal.get("quality", 0.0) or 0.0) if bool(proposal.get("eligible")) else 0.0
        proposal_setup = float(snapshot.get("setup_strength", 0.0) or 0.0)
        pacing_kwargs = dict(
            snapshot=snapshot, proposal_quality=proposal_quality, proposal_setup=proposal_setup,
            window_hours=pacing_window_hours,
            borrow_calls=int(cfg.get("futures_opportunity_borrow_calls", 1) or 0),
            borrow_min_quality=float(cfg.get("futures_opportunity_borrow_min_quality", 0.84) or 0.84),
            borrow_min_setup=float(cfg.get("futures_opportunity_borrow_min_setup", 0.68) or 0.68),
            borrow_min_heat=float(cfg.get("futures_opportunity_borrow_min_heat", 0.65) or 0.65),
            exceptional_quality=float(cfg.get("futures_opportunity_exceptional_quality", 0.92) or 0.92),
            day_session_start_utc=int(cfg.get("futures_day_session_start_utc", 6) or 6),
            day_session_end_utc=int(cfg.get("futures_day_session_end_utc", 21) or 21),
            exceptional_burst_calls=int(cfg.get("futures_day_session_exceptional_burst_calls", 1) or 0),
        )
        if pacing_enabled and opportunity_enabled:
            normal_pacing = session_opportunity_aware_paced_call_cap(int(cfg.get("futures_entry_verify_calls_daily", 10)), lane="normal", **pacing_kwargs)
            reserve_pacing = session_opportunity_aware_paced_call_cap(int(cfg.get("futures_entry_reserve_calls_daily", 8)), lane="reserve", **pacing_kwargs)
        elif pacing_enabled:
            normal_pacing = paced_daily_call_cap(int(cfg.get("futures_entry_verify_calls_daily", 10)), lane="normal", window_hours=pacing_window_hours)
            reserve_pacing = paced_daily_call_cap(int(cfg.get("futures_entry_reserve_calls_daily", 8)), lane="reserve", window_hours=pacing_window_hours)
        else:
            normal_pacing, reserve_pacing = {}, {}
        opportunity_telemetry = {
            "enabled": opportunity_enabled,
            "proposal_quality": round(proposal_quality, 4),
            "proposal_setup": round(proposal_setup, 4),
            "normal_min_quality": float(cfg.get("futures_ai_normal_min_quality", 0.70) or 0.70),
            "reserve_min_quality": float(cfg.get("futures_ai_reserve_min_quality", 0.82) or 0.82),
            "borrow_active": bool(normal_pacing.get("opportunity_borrow_active") or reserve_pacing.get("opportunity_borrow_active")),
            "market_heat": float(normal_pacing.get("opportunity_heat", reserve_pacing.get("opportunity_heat", 0.0)) or 0.0),
            "session_phase": str(normal_pacing.get("session_phase", "")),
            "day_session_active": bool(normal_pacing.get("day_session_active") or reserve_pacing.get("day_session_active")),
            "session_exceptional_burst_active": bool(normal_pacing.get("session_exceptional_burst_active") or reserve_pacing.get("session_exceptional_burst_active")),
            "policy": "overnight AI is conserved for London/New York; exceptional daytime proposals may use bounded future capacity; daily caps unchanged",
        }
        self._update(ai_entry_pacing={"enabled": pacing_enabled, "normal": normal_pacing, "reserve": reserve_pacing, "opportunity": opportunity_telemetry})

        proposal_kind = "futures_manual_diagnostic"
        proposal_cooldown_key = f"futures-manual:{symbol}:{interval}"
        if should_ai:
            if use_cached_approval:
                proposal_kind = "futures_entry_cached_approval"
                governor_reason = "reusing recent AI APPROVE for unchanged proposal signature"
                bump_proposal_stat("created", lane="futures", symbol=symbol, extra={"quality": proposal.get("quality"), "priority": proposal.get("priority"), "action": proposal.get("action")})
                bump_proposal_stat("ai_reused", lane="futures", symbol=symbol, extra={"confidence": cached_approval.get("confidence"), "model": cached_approval.get("model")})
            elif bool(proposal.get("eligible")):
                high_priority = str(proposal.get("priority", "normal")) == "high"
                normal_min_quality = float(cfg.get("futures_ai_normal_min_quality", 0.70) or 0.70)
                reserve_min_quality = float(cfg.get("futures_ai_reserve_min_quality", 0.82) or 0.82)
                if opportunity_enabled and proposal_quality < normal_min_quality and not force_ai:
                    should_ai = False
                    governor_reason = (
                        f"opportunity governor: proposal q={proposal_quality:.2f} below paid-AI floor "
                        f"{normal_min_quality:.2f}; preserving verification capacity"
                    )
                    record_cycle(symbol, interval, mode, snapshot, f"{snapshot.get('local_bias')} strength={setup:.2f}; proposal {str(proposal.get('action')).upper()} q={proposal_quality:.2f}/{proposal.get('priority')}", False)
                    self._update(state="monitoring", message=f"Monitoring {symbol}: opportunity governor preserving AI capacity; {governor_reason}")
                    return {"snapshot": snapshot, "ai_called": False, "reason": governor_reason, "mode": mode}
                proposal_kind = "futures_entry_verify"
                proposal_cooldown_key = f"futures-proposal:{symbol}:{interval}"
                signature = str(proposal.get("signature", ""))
                estimated = 2600 if high_priority else 2200
                if force_ai:
                    request_provider_probe()
                # v4.6.4: every proposal uses the normal pool first. The reserve is only
                # touched after the normal daily pool is exhausted and only for high quality.
                allowed_ai, governor_reason = reserve_ai_call(
                    "futures_entry_verify",
                    budget=budget, estimated_tokens=estimated, max_calls=int(cfg.get("ai_max_calls_daily", 0)),
                    kind_budget=int(cfg.get("futures_entry_verify_tokens_daily", 28000)),
                    kind_max_calls=int(cfg.get("futures_entry_verify_calls_daily", 10)),
                    kind_paced_max_calls=int(normal_pacing.get("paced_max_calls", 0) or 0),
                    kind_pacing_next_unlock=str(normal_pacing.get("next_unlock_at", "") or ""),
                    cooldown_key=proposal_cooldown_key,
                    cooldown_seconds=int(cfg.get("proposal_reverify_minutes", 45)) * 60,
                    signature=signature, ignore_cooldown=bool(force_ai),
                )
                reserve_candidate = high_priority and proposal_quality >= reserve_min_quality
                if (not allowed_ai and reserve_candidate and any(x in str(governor_reason).lower() for x in ("cap reached", "pacing cap reached", "token budget reached", "token reserve would exceed budget"))):
                    proposal_kind = "futures_entry_reserve"
                    allowed_ai, governor_reason = reserve_ai_call(
                        "futures_entry_reserve",
                        budget=budget, estimated_tokens=estimated, max_calls=int(cfg.get("ai_max_calls_daily", 0)),
                        kind_budget=int(cfg.get("futures_entry_reserve_tokens_daily", 22000)),
                        kind_max_calls=int(cfg.get("futures_entry_reserve_calls_daily", 8)),
                        kind_paced_max_calls=int(reserve_pacing.get("paced_max_calls", 0) or 0),
                        kind_pacing_next_unlock=str(reserve_pacing.get("next_unlock_at", "") or ""),
                        cooldown_key=f"futures-reserve:{symbol}:{interval}",
                        cooldown_seconds=int(cfg.get("proposal_reverify_minutes", 45)) * 60,
                        signature=signature, ignore_cooldown=bool(force_ai),
                    )
                if not allowed_ai:
                    should_ai = False
                else:
                    bump_proposal_stat("created", lane="futures", symbol=symbol, extra={"quality": proposal.get("quality"), "priority": proposal.get("priority"), "action": proposal.get("action"), "ai_lane": proposal_kind})
            else:
                kind_budget = 7000
                kind_calls = 2
                signature = analysis_key
                estimated = 2800
                if force_ai:
                    request_provider_probe()
                allowed_ai, governor_reason = reserve_ai_call(
                    proposal_kind, budget=budget, estimated_tokens=estimated, max_calls=int(cfg.get("ai_max_calls_daily", 0)),
                    kind_budget=kind_budget, kind_max_calls=kind_calls, cooldown_key=proposal_cooldown_key,
                    cooldown_seconds=60, signature=signature, ignore_cooldown=bool(force_ai),
                )
                if not allowed_ai:
                    should_ai = False
        elif budget > 0 and trading_tokens_today() >= budget:
            governor_reason = f"daily token budget {budget:,} reached"

        local_signal=f"{snapshot.get('local_bias')} strength={setup:.2f}"
        if bool(proposal.get("eligible")):
            local_signal += f"; proposal {str(proposal.get('action')).upper()} q={float(proposal.get('quality',0) or 0):.2f}/{proposal.get('priority')}"
        if not should_ai:
            record_cycle(symbol,interval,mode,snapshot,local_signal,False)
            self._update(state="monitoring", message=f"Monitoring {symbol}: {local_signal}; AI governor: {governor_reason}")
            return {"snapshot":snapshot,"ai_called":False,"reason":governor_reason,"mode":mode}

        # v4.6.1: do not pay for web/news context just to produce another HOLD.
        # First obtain a compact market-only decision; news verification is reserved for an actual LONG/SHORT candidate.
        include_news=False

        if self._stop_requested() or runtime_stop_requested():
            return self._abort_result("before AI analysis")
        self._update(
            state="ai_analysis",
            message=f"Futures Analyst evaluating {symbol}..." + (" + news" if include_news else ""),
            current_analysis_key=analysis_key,
        )
        research_context=research_context_for_symbol(symbol, interval, limit=4)
        adaptive_matches = live_adaptive_matches(snapshot)
        if adaptive_matches:
            research_context.append({
                "type": "adaptive_current_regime_matches",
                "matches": adaptive_matches,
                "note": "These are locally compiled hypotheses; use them only as supporting evidence, never as a standalone trade trigger.",
            })
        if bool(cfg.get("promotion_intelligence_enabled", True)):
            research_context.extend(promotion_context_for_symbol(symbol))
        try:
            if use_cached_approval:
                assessment = {
                    "action": str(proposal.get("action", "hold")),
                    "confidence": float(cached_approval.get("confidence", 0.70) or 0.70),
                    "thesis": "Reused recent AI APPROVE because the deterministic proposal signature is unchanged.",
                    "entry": float(proposal.get("entry", 0.0) or 0.0),
                    "stop_loss": float(proposal.get("stop_loss", 0.0) or 0.0),
                    "take_profit": float(proposal.get("take_profit", 0.0) or 0.0),
                    "horizon": "intraday", "catalysts": [],
                    "invalidation": "Deterministic proposal stop or structural signature change.",
                    "risk_notes": ["AI approval reused without a new paid call; Risk Engine and live execution checks remain authoritative."],
                    "used_news": False, "regime": "cached structural approval",
                    "strategy_alignment": "unchanged proposal signature", "evidence": list(proposal.get("factors") or []),
                    "proposal_verdict": "approved_cached",
                }
                usage = {"total_tokens": 0}
                model = str(cached_approval.get("model") or "cached-approval")
            else:
                assessment, usage, model=analyze_snapshot(snapshot,include_news=include_news,research_context=research_context,trade_proposal=proposal if bool(proposal.get("eligible")) else None)
        except RuntimeStoppedError:
            self._update(current_analysis_key="", state="manual_stop", message="STOPPED MANUALLY during AI analysis")
            return self._abort_result("AI analysis")
        except Exception as exc:
            text=f"{type(exc).__name__}: {exc}"
            if is_provider_availability_error(exc):
                # Billing/provider availability is not a market-evidence failure. Do not poison
                # this candle or the evidence cooldown; it may be retried after provider recovery.
                release_ai_reservation(proposal_cooldown_key)
                self._update(
                    current_analysis_key="", last_error="", state="monitoring",
                    message=f"AI PAUSED — provider unavailable; deterministic monitoring continues for {symbol}",
                )
                log_event("trading.ai_provider_paused", {"analysis_key": analysis_key, "error": text})
                record_cycle(symbol,interval,mode,snapshot,local_signal,False)
                return {"snapshot":snapshot,"ai_called":False,"ai_paused":True,"analysis_key":analysis_key,"error":text,"mode":mode}
            set_state("last_failed_analysis_key", analysis_key)
            set_state("last_failed_analysis_at", _utcnow().isoformat())
            self._update(
                current_analysis_key="",
                last_failed_analysis_key=analysis_key,
                last_error=text,
                state="monitoring",
                message=f"Futures Analyst failed safely for {symbol}; short retry cooldown active",
            )
            log_event("trading.ai_analysis.error", {"analysis_key": analysis_key, "error": text})
            record_cycle(symbol,interval,mode,snapshot,local_signal,True)
            return {"snapshot":snapshot,"ai_called":True,"ai_failed":True,"analysis_key":analysis_key,"error":text,"mode":mode}
        finally:
            if not self._stop_requested():
                self._update(current_analysis_key="")
        assessment["research_context_count"]=len(research_context)
        if self._stop_requested() or runtime_stop_requested():
            return self._abort_result("after AI analysis")
        if not use_cached_approval:
            record_trading_tokens(int(usage.get("total_tokens",0)), kind=proposal_kind)

        # Enforce deterministic proposal geometry. AI can APPROVE the exact proposal or VETO
        # with HOLD; it cannot silently invent a different direction/stop/target.
        if bool(proposal.get("eligible")):
            if not use_cached_approval:
                bump_proposal_stat("ai_verified", lane="futures", symbol=symbol)
            proposed_action = str(proposal.get("action", "hold")).lower()
            ai_action = str(assessment.get("action", "hold") or "hold").lower()
            assessment["trade_proposal"] = proposal
            if ai_action == proposed_action:
                assessment["action"] = proposed_action
                assessment["entry"] = float(proposal.get("entry", snapshot.get("price", 0.0)) or 0.0)
                assessment["stop_loss"] = float(proposal.get("stop_loss", 0.0) or 0.0)
                assessment["take_profit"] = float(proposal.get("take_profit", 0.0) or 0.0)
                assessment["proposal_verdict"] = "approved_cached" if use_cached_approval else "approved"
                clear_proposal_veto(symbol, interval, "futures")
                if not use_cached_approval:
                    record_proposal_approval(
                        symbol, interval, signature=str(proposal.get("signature", "")), action=proposed_action,
                        confidence=float(assessment.get("confidence", 0.0) or 0.0), model=str(model),
                        minutes=int(cfg.get("proposal_approval_minutes", 45)), lane="futures",
                    )
                    bump_proposal_stat("ai_approved", lane="futures", symbol=symbol, extra={"quality": proposal.get("quality")})
            else:
                assessment = dict(assessment)
                assessment["action"] = "hold"
                assessment["entry"] = float(snapshot.get("price", 0.0) or 0.0)
                assessment["stop_loss"] = 0.0
                assessment["take_profit"] = 0.0
                assessment["proposal_verdict"] = "vetoed"
                assessment["trade_proposal"] = proposal
                veto_text = str(assessment.get("thesis") or assessment.get("invalidation") or "AI vetoed deterministic proposal")
                clear_proposal_approval(symbol, interval, "futures")
                record_proposal_veto(symbol, interval, signature=str(proposal.get("signature", "")), reason=veto_text, action=proposed_action, minutes=int(cfg.get("proposal_veto_minutes", 90)), lane="futures")
                bump_proposal_stat("ai_vetoed", lane="futures", symbol=symbol, extra={"quality": proposal.get("quality"), "reason": veto_text[:240]})

        # Late news verification: only a real entry candidate is worth a second, web-enabled call.
        action_now = str(assessment.get("action", "hold") or "hold").lower()
        if action_now in {"long", "short"} and bool(cfg.get("news_enabled", True)) and bool(cfg.get("ai_news_verify_only_after_entry", True)):
            last_news=_parse_iso(get_state("last_news_analysis", ""))
            cooldown=timedelta(minutes=int(cfg.get("news_cooldown_minutes",180)))
            if force_ai or last_news is None or _utcnow()-last_news >= cooldown:
                try:
                    news_allowed, news_reason = reserve_ai_call(
                        "futures_news_verify",
                        kind_budget=int(cfg.get("futures_news_tokens_daily", 15000)),
                        kind_max_calls=int(cfg.get("futures_news_calls_daily", 3)),
                        cooldown_key=f"futures-news:{symbol}:{interval}",
                        cooldown_seconds=int(cfg.get("news_cooldown_minutes", 180)) * 60,
                        signature=analysis_key,
                        ignore_cooldown=bool(force_ai),
                    )
                    if not news_allowed:
                        raise RuntimeError(f"news verification governor: {news_reason}")
                    verified, news_usage, news_model = analyze_snapshot(snapshot, include_news=True, research_context=research_context, trade_proposal=proposal if bool(proposal.get("eligible")) else None)
                    record_trading_tokens(int(news_usage.get("total_tokens",0)), kind="futures_news_verify")
                    verified["pre_news_action"] = action_now
                    if bool(proposal.get("eligible")) and str(verified.get("action", "hold")).lower() == str(proposal.get("action", "hold")).lower():
                        verified["entry"] = float(proposal.get("entry", 0.0) or 0.0)
                        verified["stop_loss"] = float(proposal.get("stop_loss", 0.0) or 0.0)
                        verified["take_profit"] = float(proposal.get("take_profit", 0.0) or 0.0)
                        verified["proposal_verdict"] = "approved_after_news"
                        verified["trade_proposal"] = proposal
                    elif bool(proposal.get("eligible")):
                        verified["action"] = "hold"
                        verified["entry"] = float(snapshot.get("price",0.0) or 0.0)
                        verified["stop_loss"] = 0.0
                        verified["take_profit"] = 0.0
                        verified["proposal_verdict"] = "vetoed_after_news"
                        verified["trade_proposal"] = proposal
                        record_proposal_veto(symbol, interval, signature=str(proposal.get("signature", "")), reason=str(verified.get("thesis") or "news veto"), action=str(proposal.get("action", "")), minutes=max(120, int(cfg.get("proposal_veto_minutes",90))), lane="futures")
                    assessment = verified
                    model = news_model
                    include_news = True
                    set_state("last_news_analysis",_utcnow().isoformat())
                except RuntimeStoppedError:
                    return self._abort_result("news verification")
                except Exception as news_exc:
                    if is_provider_availability_error(news_exc):
                        release_ai_reservation(proposal_cooldown_key)
                    assessment = dict(assessment)
                    assessment.setdefault("risk_notes", []).append(f"news verification unavailable/skipped: {type(news_exc).__name__}")
                    assessment["news_verification_skipped"] = True
                    assessment["news_verification_reason"] = f"{type(news_exc).__name__}: {news_exc}"
                    if bool(cfg.get("futures_news_fail_closed", False)):
                        assessment["action"] = "hold"
                        assessment["confidence"] = min(float(assessment.get("confidence",0) or 0), 0.50)
                        assessment["thesis"] = "Entry candidate withheld because required current-news/provider verification was unavailable. " + str(assessment.get("thesis", ""))
                    log_event("trading.news_verification.error", {"analysis_key": analysis_key, "error": f"{type(news_exc).__name__}: {news_exc}", "fail_closed": bool(cfg.get("futures_news_fail_closed", False))})

        set_state(candle_key,str(closed_candle))
        set_state(heartbeat_key,"0")
        set_state("last_completed_analysis_key", analysis_key)
        set_state("last_failed_analysis_key", "")
        set_state("last_failed_analysis_at", "")
        assessment["model"]=model
        assessment["include_news"]=include_news
        # Bind every AI decision to the exact symbol/candle that produced it.  The market
        # scanner may rotate to another symbol on the next poll; without this identity the
        # UI can accidentally display a valid CYS/HEMI decision under a newer SOL snapshot.
        assessment["analysis_key"] = analysis_key
        assessment["analysis_symbol"] = symbol
        assessment["analysis_interval"] = interval
        assessment["analysis_closed_candle_start_ms"] = closed_candle
        assessment["analysis_signal_price"] = snapshot.get("signal_price", snapshot.get("price"))
        assessment["analysis_live_price"] = snapshot.get("price")
        assessment["strategy_support"] = strategy_state
        self._update(last_assessment=assessment, last_completed_analysis_key=analysis_key, last_failed_analysis_key="", last_error="")

        instrument=selected_instrument or BybitClient(testnet=bool(cfg.get("market_data_testnet",False))).get_instrument(symbol)
        equity=None
        open_positions=0
        private_client=None
        key_env=str(cfg.get("bybit_key_environment","auto"))
        learning_state: dict[str, Any] = {
            "daily_pnl": None, "weekly_pnl": None, "trades_today": 0, "risk_multiplier": 1.0,
            "target_risk_pct": float(cfg.get("risk_per_trade_pct", 0.25)),
            "leverage_cap": float(cfg.get("max_leverage", 3.0)),
            "exposure_cap_pct": float(cfg.get("max_notional_pct_equity", 75.0)),
            "max_trades_today_allowed": int(cfg.get("max_trades_per_day", 16)),
            "max_positions_allowed": int(cfg.get("max_positions", 1)),
            "growth_stage": "static", "performance_metrics": {},
            "confidence_bump": 0.0, "pause": False, "notes": [],
            "portfolio_risk_cap_pct": float(cfg.get("portfolio_learning_risk_cap_pct", 25.0)),
        }
        if mode == "testnet" and has_bybit_credentials():
            private_client=BybitClient(testnet=True,authenticated=True)
            equity=_wallet_equity(private_client)
            open_positions=len(_open_positions_all(private_client))
        elif mode in {"shadow", "autopilot_live"} and has_bybit_credentials():
            private_client=live_state_client or BybitClient(testnet=False,authenticated=True)
            equity=_wallet_equity(private_client)
            open_positions=len(_open_positions_all(private_client))
            if mode == "autopilot_live" and equity and equity > 0:
                learning_state=live_learning_snapshot(private_client,equity)
                assessment["live_learning_state"] = learning_state
                set_state("current_learning_state", json.dumps(learning_state, ensure_ascii=False)[:16000])
                set_state("current_learning_state_updated_at", _utcnow().isoformat(timespec="seconds"))
                self._update(current_learning_state=learning_state)
        elif mode == "paper":
            open_positions=1 if get_paper_position(symbol) else 0

        portfolio = {
            "open_positions": open_positions,
            "estimated_open_risk_pct": 0.0,
            "same_symbol_open": False,
            "unprotected_positions": [],
            "max_directional_correlation": {},
        }
        if private_client is not None and equity and equity > 0 and mode in {"testnet", "autopilot_live"}:
            try:
                portfolio = portfolio_state(
                    private_client,
                    candidate_symbol=symbol,
                    candidate_action=str(assessment.get("action", "hold")),
                    equity=float(equity),
                    interval=interval,
                    correlation_threshold=float(cfg.get("portfolio_correlation_threshold", 0.85)),
                )
                assessment["portfolio_state"] = portfolio
                open_positions = int(portfolio.get("open_positions", open_positions) or open_positions)
            except Exception as exc:
                log_event("trading.portfolio_state.error", {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

        execution_lock = get_state("execution_safety_lock", "0") == "1"
        if execution_lock and private_client is not None and mode in {"testnet", "autopilot_live"}:
            try:
                reconcile = reconcile_execution_lock(private_client)
                execution_lock = bool(reconcile.get("locked"))
                if not execution_lock:
                    log_event("trading.execution.reconciled", reconcile)
            except Exception as exc:
                log_event("trading.execution.reconcile_error", {"error": f"{type(exc).__name__}: {exc}"})
                execution_lock = True

        risk_margin_state = dict(live_margin_state) if isinstance(live_margin_state, dict) else {}
        risk_margin_error = ""
        if mode == "autopilot_live" and private_client is not None:
            try:
                # Re-read just before sizing. This catches collateral consumed/released while AI
                # was evaluating and keeps the order inside the account's *current* free margin.
                risk_margin_state = unified_margin_state(private_client)
                self._update(live_account_capacity=risk_margin_state)
            except Exception as margin_exc:
                risk_margin_error = f"{type(margin_exc).__name__}: {margin_exc}"
                if not risk_margin_state:
                    log_event("trading.risk_account_capacity.error", {"error": risk_margin_error})
        risk_available_balance = None
        if risk_margin_state:
            try:
                risk_available_balance = max(0.0, float(risk_margin_state.get("total_available_balance_usd", 0.0) or 0.0))
            except Exception:
                risk_available_balance = None
        capacity_state_block = bool(
            mode == "autopilot_live" and open_positions > 0
            and bool(cfg.get("futures_require_capacity_state_with_open_positions", True))
            and risk_available_balance is None
        )
        if risk_margin_state:
            assessment["account_capacity"] = risk_margin_state

        if self._stop_requested():
            return self._abort_result("before risk evaluation")
        risk=evaluate_trade_candidate(
            assessment,snapshot,instrument,equity=equity,open_positions=open_positions,
            daily_realized_pnl=learning_state.get("daily_pnl"),
            weekly_realized_pnl=learning_state.get("weekly_pnl"),
            trades_today=int(learning_state.get("trades_today",0) or 0),
            learning_risk_multiplier=float(learning_state.get("risk_multiplier",1.0) or 0.0),
            adaptive_risk_pct=float(learning_state.get("target_risk_pct", cfg.get("risk_per_trade_pct",0.25)) or 0.0),
            leverage_cap=float(learning_state.get("leverage_cap", cfg.get("max_leverage",3.0)) or 1.0),
            exposure_cap_pct=float(learning_state.get("exposure_cap_pct", cfg.get("max_notional_pct_equity",75.0)) or 0.0),
            max_trades_today_allowed=int(learning_state.get("max_trades_today_allowed", cfg.get("max_trades_per_day",16)) or 1),
            max_positions_allowed=int(learning_state.get("max_positions_allowed", cfg.get("max_positions",1)) or 1),
            growth_stage=str(learning_state.get("growth_stage","static")),
            performance_metrics=dict(learning_state.get("performance_metrics") or {}),
            confidence_bump=float(learning_state.get("confidence_bump",0.0) or 0.0) + float(strategy_state.get("confidence_bump_if_unsupported", 0.0) or 0.0),
            safety_pause=bool(learning_state.get("pause",False)) or execution_lock or capacity_state_block,
            learning_notes=list(learning_state.get("notes") or []) + (["execution safety lock active; no new entries until Bybit order state is reconciled"] if execution_lock else []) + (["current Bybit available balance could not be verified while an existing Futures position is open"] if capacity_state_block else []) + (["no currently approved OOS strategy support for this symbol/timeframe; extra selectivity applied"] if not strategy_state.get("supported") else []),
            portfolio_open_risk_pct=float(portfolio.get("estimated_open_risk_pct", 0.0) or 0.0),
            portfolio_risk_cap_pct=float(learning_state.get("portfolio_risk_cap_pct", cfg.get("portfolio_learning_risk_cap_pct", 25.0)) or 25.0),
            same_symbol_open=bool(portfolio.get("same_symbol_open", False)),
            unprotected_positions=list(portfolio.get("unprotected_positions") or []),
            max_directional_correlation=dict(portfolio.get("max_directional_correlation") or {}),
            available_balance_usd=risk_available_balance,
        )
        risk["analysis_key"] = analysis_key
        risk["analysis_symbol"] = symbol
        risk["analysis_interval"] = interval
        risk["analysis_closed_candle_start_ms"] = closed_candle
        self._update(last_risk=risk,state="risk",message="Risk engine: " + ("PASS" if risk["allowed"] else "BLOCK"))
        if risk.get("allowed") and bool(proposal.get("eligible")):
            bump_proposal_stat("risk_passed", lane="futures", symbol=symbol, extra={"risk_cash": risk.get("risk_cash"), "rr": risk.get("reward_risk")})
            if bool(risk.get("margin_resized")):
                bump_proposal_stat("capacity_resized", lane="futures", symbol=symbol, extra={
                    "available_balance_usd": risk.get("account_available_balance_usd"),
                    "margin_budget_usd": risk.get("margin_budget_usd"),
                    "notional_usdt": risk.get("notional_usdt"),
                    "leverage": risk.get("leverage"),
                    "reason": risk.get("margin_resize_reason"),
                })
        execution: dict[str,Any]={"mode":mode,"executed":False}

        if risk["allowed"]:
            if self._stop_requested():
                return self._abort_result("before execution")
            if mode == "paper":
                pos=open_paper_position(symbol,risk["action"],float(risk["qty"]),float(risk["entry"]),float(risk["stop_loss"]),float(risk["take_profit"]))
                execution={"mode":"paper","executed":True,"position":pos}
                log_event("trading.paper.open", execution)
            elif mode == "shadow":
                execution={"mode":"shadow","executed":False,"candidate":risk,"message":"Shadow mode: valid candidate recorded, no order sent."}
            elif mode == "testnet":
                if not has_bybit_credentials():
                    execution={"mode":"testnet","executed":False,"error":"Bybit Testnet credentials not configured."}
                else:
                    assert private_client is not None
                    if self._stop_requested():
                        return self._abort_result("before testnet order")
                    side="Buy" if risk["action"]=="long" else "Sell"
                    execution = _submit_confirm_market_entry(
                        private_client, mode="testnet", symbol=symbol, side=side, risk=risk,
                        snapshot=snapshot, cfg=cfg, order_prefix="stantest",
                    )
                    confirmation = dict(execution.get("confirmation") or {})
                    if bool(execution.get("submitted")):
                        bump_proposal_stat("submitted", lane="futures", symbol=symbol, extra={
                            "order_id": (execution.get("response") or {}).get("orderId"),
                            "order_link_id": execution.get("order_link_id"),
                            "slippage_tolerance_pct": execution.get("slippage_tolerance_pct"),
                        })
                    if bool(execution.get("confirmed")):
                        bump_proposal_stat("confirmed", lane="futures", symbol=symbol, extra={
                            "lifecycle": confirmation.get("lifecycle"), "position_open": confirmation.get("position_open"),
                            "protected": confirmation.get("protected"), "cum_exec_qty": confirmation.get("cum_exec_qty"),
                        })
                    elif bool(confirmation.get("uncertain")):
                        bump_proposal_stat("execution_uncertain", lane="futures", symbol=symbol, extra={"reason": confirmation.get("reason")})
                    else:
                        bump_proposal_stat("execution_failed", lane="futures", symbol=symbol, extra={
                            "status": confirmation.get("order_status"), "lifecycle": confirmation.get("lifecycle"),
                            "reason": confirmation.get("reason") or execution.get("submit_error"),
                        })
                    log_event("trading.testnet.order", {"symbol":symbol,"risk":risk,"execution":execution})
            elif mode == "autopilot_live":
                guard=validate_autopilot_key()
                if not bool(guard.get("live_armed")):
                    execution={"mode":"autopilot_live","executed":False,"error":str(guard.get("blocked_reason") or guard.get("message") or "Mainnet live key is not safely armed.")}
                    bump_proposal_stat("execution_failed", lane="futures", symbol=symbol, extra={"stage":"live_guard","reason":execution.get("error")})
                else:
                    assert private_client is not None
                    if self._stop_requested():
                        return self._abort_result("before live order")
                    side="Buy" if risk["action"]=="long" else "Sell"
                    execution = _submit_confirm_market_entry(
                        private_client, mode="autopilot_live", symbol=symbol, side=side, risk=risk,
                        snapshot=snapshot, cfg=cfg, order_prefix="stanlive",
                    )
                    execution["learning_state"] = learning_state
                    confirmation = dict(execution.get("confirmation") or {})
                    if bool(execution.get("submitted")):
                        bump_proposal_stat("submitted", lane="futures", symbol=symbol, extra={
                            "order_id": (execution.get("response") or {}).get("orderId"),
                            "order_link_id": execution.get("order_link_id"),
                            "slippage_tolerance_pct": execution.get("slippage_tolerance_pct"),
                        })
                    if bool(execution.get("confirmed")):
                        bump_proposal_stat("confirmed", lane="futures", symbol=symbol, extra={
                            "lifecycle": confirmation.get("lifecycle"), "position_open": confirmation.get("position_open"),
                            "protected": confirmation.get("protected"), "cum_exec_qty": confirmation.get("cum_exec_qty"),
                        })
                    elif bool(confirmation.get("uncertain")):
                        bump_proposal_stat("execution_uncertain", lane="futures", symbol=symbol, extra={"reason": confirmation.get("reason")})
                    else:
                        bump_proposal_stat("execution_failed", lane="futures", symbol=symbol, extra={
                            "status": confirmation.get("order_status"), "lifecycle": confirmation.get("lifecycle"),
                            "reason": confirmation.get("reason") or execution.get("submit_error"),
                        })
                    log_event("trading.mainnet.order", {"symbol":symbol,"risk":risk,"execution":execution})
            else:
                execution={"mode":"observer","executed":False,"message":"Observer mode: analysis only."}
        else:
            execution={"mode":mode,"executed":False,"message":"Risk engine blocked trade.","reasons":risk["reasons"]}

        final_confirmation = execution.get("confirmation") if isinstance(execution.get("confirmation"), dict) else {}
        capacity_reject_reason = str((final_confirmation or {}).get("reason") or execution.get("submit_error") or "")
        if "110007" in capacity_reject_reason or "ab not enough" in capacity_reject_reason.lower():
            bump_proposal_stat("capacity_rejected", lane="futures", symbol=symbol, extra={
                "reason": capacity_reject_reason[:400],
                "available_balance_after_reject": ((final_confirmation or {}).get("account_capacity_after_reject") or {}).get("total_available_balance_usd") if isinstance((final_confirmation or {}).get("account_capacity_after_reject"), dict) else None,
                "risk_notional_usdt": risk.get("notional_usdt"),
                "risk_leverage": risk.get("leverage"),
            })

        execution["analysis_key"] = analysis_key
        execution["analysis_symbol"] = symbol
        execution["analysis_interval"] = interval
        execution["analysis_closed_candle_start_ms"] = closed_candle
        if bool(execution.get("executed")) and bool(execution.get("confirmed")) and bool(proposal.get("eligible")):
            bump_proposal_stat("executed", lane="futures", symbol=symbol, extra={
                "mode": mode, "confirmed": True,
                "position_open": (execution.get("confirmation") or {}).get("position_open") if isinstance(execution.get("confirmation"), dict) else None,
                "order_link_id": execution.get("order_link_id"),
            })
        record_cycle(symbol,interval,mode,snapshot,local_signal,True)
        record_assessment(symbol,closed_candle,assessment,risk,execution)
        self._update(last_execution=execution,state="monitoring",message=f"{symbol}: {assessment['action'].upper()} {assessment['confidence']:.0%}; risk {'PASS' if risk['allowed'] else 'BLOCK'}")
        return {"snapshot":snapshot,"assessment":assessment,"risk":risk,"execution":execution,"usage":usage,"mode":mode}


TRADING_CONTROLLER=TradingController()


def test_bybit_private_connection(*, testnet: bool = True) -> dict[str, Any]:
    client=BybitClient(testnet=testnet,authenticated=True)
    key_info=client.get_api_key_info()
    wallet=client.get_wallet_balance("USDT")
    positions=client.get_positions(settle_coin="USDT")
    permissions=key_info.get("permissions") or {}
    return {
        "ok":True,
        "testnet":testnet,
        "wallet":wallet,
        "open_positions":len([p for p in positions if float(p.get('size') or 0)>0]),
        "read_only":int(key_info.get("readOnly",-1) or 0)==1,
        "permissions":permissions,
        "note":str(key_info.get("note", "")),
    }
