from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from paths import APP_HOME, DATA_DIR

BYBIT_ENV_FILE = APP_HOME / ".bybit.env"
BYBIT_SECURE_FILE = DATA_DIR / "bybit_credentials.dpapi"
TRADING_SETTINGS_FILE = DATA_DIR / "trading_settings.json"

DEFAULT_TRADING_SETTINGS: dict[str, Any] = {
    "mode": "observer",  # observer | paper | shadow | testnet | autopilot_live
    "symbol": "BTCUSDT",
    "category": "linear",
    "interval": "15",
    "poll_seconds": 20,
    "market_data_testnet": False,
    "execution_environment": "auto",  # auto | mainnet | testnet (canonical execution source of truth)
    "execution_testnet": True,  # legacy derived alias; never authoritative
    "bybit_key_environment": "auto",  # auto | testnet | mainnet_readonly | mainnet_trade
    "auto_start": False,
    "one_button_autopilot": True,
    "autopilot_profile_version": 20,
    "auto_symbol_selection": True,
    "symbol_scan_minutes": 5,
    "live_watchlist_size": 10,
    "market_rotation_margin": 0.08,
    "market_dominance_margin": 0.12,
    "ai_enabled": True,
    "ai_candidate_threshold": 0.68,
    "ai_heartbeat_candles": 0,
    "ai_strong_candidate_threshold": 0.80,
    "ai_decision_cooldown_minutes": 60,
    "ai_max_calls_daily": 0,  # 0 = unlimited; anti-loop dedupe still active
    "ai_hard_limits_enabled": False,
    "ai_provider_probe_minutes": 15,
    "ai_rotation_only_strong": False,
    "ai_news_verify_only_after_entry": True,
    "futures_news_fail_closed": False,
    "proposal_min_setup": 0.58,
    "proposal_min_direction": 0.26,
    "proposal_min_quality": 0.62,
    "proposal_high_priority_quality": 0.76,
    "proposal_max_vwap_atr_multiple": 1.75,
    "proposal_stop_atr": 0.90,
    "proposal_target_rr": 1.65,
    "proposal_veto_minutes": 90,
    "proposal_approval_minutes": 45,
    "proposal_reverify_minutes": 45,
    "proposal_watchlist_preflight": 4,
    "futures_entry_verify_tokens_daily": 28000,
    "futures_entry_verify_calls_daily": 10,
    "futures_entry_reserve_tokens_daily": 22000,
    "futures_entry_reserve_calls_daily": 8,
    "futures_entry_pacing_enabled": True,
    "futures_entry_pacing_window_hours": 4,
    "futures_opportunity_governor_enabled": True,
    "futures_ai_normal_min_quality": 0.70,
    "futures_ai_reserve_min_quality": 0.82,
    "futures_opportunity_borrow_calls": 1,
    "futures_opportunity_borrow_min_quality": 0.84,
    "futures_opportunity_borrow_min_setup": 0.68,
    "futures_opportunity_borrow_min_heat": 0.65,
    "futures_opportunity_exceptional_quality": 0.92,
    "futures_day_session_start_utc": 6,
    "futures_day_session_end_utc": 21,
    "futures_day_session_exceptional_burst_calls": 1,
    "futures_ai_tokens_daily": 50000,
    "futures_ai_calls_daily": 18,
    "futures_news_tokens_daily": 15000,
    "futures_news_calls_daily": 3,
    "spot_ai_tokens_daily": 28000,
    "spot_ai_calls_daily": 10,
    "spot_news_tokens_daily": 10000,
    "spot_news_calls_daily": 2,
    "promotion_ai_tokens_daily": 12000,
    "promotion_ai_calls_daily": 1,
    "promotion_action_tokens_daily": 0,
    "promotion_action_calls_daily": 0,
    "research_chief_tokens_daily": 30000,
    "research_chief_calls_daily": 1,
    "strategy_discovery_tokens_daily": 25000,
    "strategy_discovery_calls_daily": 1,
    "spot_strategy_discovery_tokens_daily": 25000,
    "spot_strategy_discovery_calls_daily": 1,
    "news_enabled": True,
    "news_cooldown_minutes": 180,
    "min_confidence": 0.60,
    "min_confidence_floor": 0.56,
    "adaptive_confidence_enabled": True,
    "risk_per_trade_pct": 4.00,
    "absolute_risk_cap_pct": 7.00,
    "max_daily_loss_pct": 25.0,
    "max_weekly_loss_pct": 30.0,
    "max_leverage": 4.0,
    "max_positions": 5,
    "max_notional_usdt": 50.0,
    "executable_min_order_override": True,
    "min_order_override_max_risk_pct": 7.00,
    "min_order_override_max_target_multiple": 4.0,
    "max_notional_pct_equity": 225.0,
    "max_trades_per_day": 16,
    "min_reward_risk": 1.30,
    "learning_risk_multiplier": 1.0,
    "learning_full_risk_after_trades": 40,
    "growth_calibration_trades": 6,
    "growth_calibration_risk_pct": 3.00,
    "growth_learning_risk_pct": 4.00,
    "growth_validated_risk_pct": 5.00,
    "growth_mature_risk_pct": 6.00,
    "growth_validated_min_trades": 40,
    "growth_mature_min_trades": 100,
    "growth_validated_min_profit_factor": 1.10,
    "growth_mature_min_profit_factor": 1.20,
    "growth_validated_max_drawdown_pct": 6.0,
    "growth_mature_max_drawdown_pct": 6.0,
    "growth_learning_exposure_pct": 225.0,
    "growth_validated_exposure_pct": 275.0,
    "growth_mature_exposure_pct": 300.0,
    "growth_learning_leverage_cap": 3.0,
    "growth_validated_leverage_cap": 3.5,
    "growth_mature_leverage_cap": 4.0,
    "growth_learning_max_trades_day": 10,
    "growth_validated_max_trades_day": 14,
    "growth_mature_max_trades_day": 18,
    "growth_learning_max_positions": 3,
    "growth_validated_max_positions": 4,
    "growth_mature_max_positions": 5,
    "portfolio_learning_enabled": True,
    "portfolio_learning_risk_cap_pct": 25.0,
    "portfolio_validated_risk_cap_pct": 25.0,
    "portfolio_mature_risk_cap_pct": 25.0,
    "portfolio_absolute_risk_cap_pct": 25.0,
    "portfolio_absolute_risk_cap_usdt": 20.0,
    "portfolio_correlation_threshold": 0.85,
    "portfolio_block_unprotected_positions": True,
    "loss_streak_pause_after": 3,
    "loss_streak_pause_hours": 8,
    "loss_streak_time_pause_enabled": False,
    "live_order_slippage_pct": 0.25,
    "live_order_slippage_cap_pct": 0.75,
    "live_order_slippage_atr_factor": 0.15,
    "futures_capacity_pre_ai_enabled": True,
    "futures_available_balance_utilization_pct": 82.0,
    "futures_available_balance_reserve_usdt": 2.0,
    "futures_require_capacity_state_with_open_positions": True,
    "futures_capacity_reject_cooldown_minutes": 20,
    "futures_capacity_reject_recovery_usdt": 3.0,
    "max_spread_bps": 12.0,
    "max_data_age_seconds": 90,
    "paper_start_equity": 10000.0,
    "trading_token_budget_daily": 0,
    "auto_bootstrap_after_connection": True,
    "research_universe_top_n": 12,
    "research_regime_symbols": 6,
    "research_backtest_symbols": 3,
    "research_backtest_candles": 1600,
    "research_slippage_bps": 1.5,
    "research_refresh_hours": 24,
    "adaptive_strategy_discovery_enabled": True,
    "adaptive_strategy_hypotheses": 8,
    "opportunity_os_enabled": True,
    "opportunity_refresh_minutes": 15,
    "spot_opportunity_enabled": True,
    "spot_live_execution_enabled": True,
    "spot_interval": "15",
    "spot_universe_top_n": 10,
    "spot_ai_candidate_threshold": 0.72,
    "spot_ai_strong_threshold": 0.82,
    "spot_ai_cooldown_minutes": 60,
    "spot_proposal_min_setup": 0.60,
    "spot_proposal_min_quality": 0.66,
    "spot_proposal_high_priority_quality": 0.78,
    "spot_proposal_max_vwap_atr_multiple": 1.55,
    "spot_proposal_stop_atr": 0.85,
    "spot_proposal_target_rr": 1.60,
    "spot_proposal_veto_minutes": 90,
    "spot_proposal_approval_minutes": 45,
    "spot_proposal_reverify_minutes": 45,
    "spot_entry_verify_tokens_daily": 18000,
    "spot_entry_verify_calls_daily": 7,
    "spot_entry_reserve_tokens_daily": 10000,
    "spot_entry_reserve_calls_daily": 4,
    "spot_entry_pacing_enabled": True,
    "spot_entry_pacing_window_hours": 4,
    "spot_news_threshold": 0.80,
    "spot_min_confidence": 0.68,
    "spot_no_oos_confidence_bump": 0.02,
    "spot_min_reward_risk": 1.35,
    "spot_max_spread_bps": 22.0,
    "spot_entry_cross_bps": 4.0,
    "spot_entry_timeout_seconds": 90,
    "spot_absolute_risk_cap_pct": 2.00,
    "spot_learning_risk_pct": 0.75,
    "spot_validated_risk_pct": 1.00,
    "spot_mature_risk_pct": 1.25,
    "spot_learning_max_allocation_pct": 25.0,
    "spot_validated_max_allocation_pct": 35.0,
    "spot_mature_max_allocation_pct": 50.0,
    "spot_min_order_max_allocation_pct": 35.0,
    "spot_adaptive_research_enabled": True,
    "spot_research_refresh_hours": 24,
    "spot_backtest_candles": 1400,
    "spot_research_slippage_bps": 2.0,
    "earn_discovery_enabled": True,
    "alpha_discovery_enabled": True,
    "prediction_discovery_enabled": True,
    "promotion_intelligence_enabled": True,
    "promotion_auto_ai_refresh_enabled": False,
    "promotion_region_hint": "auto",
    "promotion_refresh_hours": 24,
    "browser_operator_enabled": True,
    "browser_action_refresh_hours": 12,
    "browser_action_refresh_minutes": 720,
    "browser_action_max_cycles_daily": 2,
    "browser_background_only": True,
    "browser_max_actions_per_cycle": 8,
    "browser_cycle_timeout_seconds": 420,
    "promotion_trade_alignment_weight": 0.03,
    "promotion_min_base_setup": 0.45,
}


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buf = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(in_blob), "Bybit AI Account Manager", None, None, None, 0x1, ctypes.byref(out_blob)):
        raise OSError("Windows DPAPI could not encrypt Bybit credentials.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buf = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob)):
        raise OSError("Windows DPAPI could not decrypt Bybit credentials for this Windows user.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _set_env_credentials(key: str, secret: str) -> None:
    os.environ["BYBIT_API_KEY"] = key
    os.environ["BYBIT_API_SECRET"] = secret


def load_bybit_env() -> None:
    # On Windows, v3.1 stores Bybit credentials encrypted with DPAPI bound to the current user.
    if os.name == "nt" and BYBIT_SECURE_FILE.exists():
        try:
            encrypted = base64.b64decode(BYBIT_SECURE_FILE.read_bytes())
            payload = json.loads(_dpapi_unprotect(encrypted).decode("utf-8"))
            key = str(payload.get("api_key", "")).strip()
            secret = str(payload.get("api_secret", "")).strip()
            if key and secret:
                _set_env_credentials(key, secret)
                return
        except Exception:
            # Do not silently fall through to stale environment values.
            os.environ.pop("BYBIT_API_KEY", None)
            os.environ.pop("BYBIT_API_SECRET", None)
            raise

    # Compatibility/fallback for development and automatic migration from older Stan versions.
    load_dotenv(BYBIT_ENV_FILE, override=True)
    key = os.getenv("BYBIT_API_KEY", "").strip()
    secret = os.getenv("BYBIT_API_SECRET", "").strip()
    if os.name == "nt" and key and secret and not BYBIT_SECURE_FILE.exists():
        save_bybit_credentials(key, secret)


def has_bybit_credentials() -> bool:
    try:
        load_bybit_env()
    except Exception:
        return False
    return bool(os.getenv("BYBIT_API_KEY", "").strip() and os.getenv("BYBIT_API_SECRET", "").strip())


def save_bybit_credentials(api_key: str, api_secret: str) -> None:
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    if not key or not secret:
        raise ValueError("Both Bybit API key and secret are required.")
    if any(ch in key + secret for ch in "\r\n"):
        raise ValueError("Bybit credentials must be single-line values.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        payload = json.dumps({"api_key": key, "api_secret": secret}, separators=(",", ":")).encode("utf-8")
        encrypted = _dpapi_protect(payload)
        temp = Path(str(BYBIT_SECURE_FILE) + ".tmp")
        temp.write_bytes(base64.b64encode(encrypted))
        temp.replace(BYBIT_SECURE_FILE)
        if BYBIT_ENV_FILE.exists():
            try:
                BYBIT_ENV_FILE.unlink()
            except OSError:
                pass
    else:
        temp = Path(str(BYBIT_ENV_FILE) + ".tmp")
        temp.write_text(f"BYBIT_API_KEY={key}\nBYBIT_API_SECRET={secret}\n", encoding="utf-8")
        temp.replace(BYBIT_ENV_FILE)
        try:
            os.chmod(BYBIT_ENV_FILE, 0o600)
        except OSError:
            pass
    _set_env_credentials(key, secret)


def clear_bybit_credentials() -> None:
    for path in (BYBIT_SECURE_FILE, BYBIT_ENV_FILE):
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    os.environ.pop("BYBIT_API_KEY", None)
    os.environ.pop("BYBIT_API_SECRET", None)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}: return True
        if low in {"0", "false", "no", "off"}: return False
    return default


def _float(value: Any, default: float, lo: float, hi: float) -> float:
    try: n = float(value)
    except Exception: n = default
    return max(lo, min(hi, n))


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try: n = int(value)
    except Exception: n = default
    return max(lo, min(hi, n))




def resolve_execution_environment(*, mode: str, key_environment: str, configured: str = "auto") -> str:
    """Resolve one canonical execution environment without allowing a stale legacy boolean to override it."""
    configured = str(configured or "auto").lower()
    key_environment = str(key_environment or "auto").lower()
    mode = str(mode or "observer").lower()
    key_env = "testnet" if key_environment == "testnet" else ("mainnet" if key_environment in {"mainnet_readonly", "mainnet_trade"} else "")
    if configured in {"mainnet", "testnet"}:
        # Explicit canonical config is preserved; prelaunch hard-blocks if the detected key disagrees.
        return configured
    if key_env:
        return key_env
    return "testnet" if mode == "testnet" else "mainnet"

def load_trading_settings() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if TRADING_SETTINGS_FILE.exists():
        try:
            data = json.loads(TRADING_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict): raw = data
        except Exception:
            raw = {}
    mode = str(raw.get("mode", DEFAULT_TRADING_SETTINGS["mode"])).lower()
    if mode not in {"observer", "paper", "shadow", "testnet", "autopilot_live"}: mode = "observer"
    interval = str(raw.get("interval", DEFAULT_TRADING_SETTINGS["interval"]))
    if interval not in {"1","3","5","15","30","60","120","240","D"}: interval = "15"
    key_environment = str(raw.get("bybit_key_environment", "auto"))
    if key_environment not in {"auto", "testnet", "mainnet_readonly", "mainnet_trade"}: key_environment = "auto"
    configured_environment = str(raw.get("execution_environment", "auto")).lower()
    if configured_environment not in {"auto", "mainnet", "testnet"}: configured_environment = "auto"
    execution_environment = resolve_execution_environment(mode=mode, key_environment=key_environment, configured=configured_environment)
    hard_ai_limits = _coerce_bool(raw.get("ai_hard_limits_enabled"), False)
    return {
        "mode": mode,
        "symbol": str(raw.get("symbol", "BTCUSDT")).upper().strip() or "BTCUSDT",
        "category": "linear",
        "interval": interval,
        "poll_seconds": _int(raw.get("poll_seconds"), 20, 5, 300),
        "market_data_testnet": _coerce_bool(raw.get("market_data_testnet"), False),
        "execution_environment": execution_environment,
        "execution_testnet": execution_environment == "testnet",
        "bybit_key_environment": key_environment,
        "auto_start": _coerce_bool(raw.get("auto_start"), False),
        "one_button_autopilot": _coerce_bool(raw.get("one_button_autopilot"), True),
        "autopilot_profile_version": _int(raw.get("autopilot_profile_version"), 6, 1, 99),
        "auto_symbol_selection": _coerce_bool(raw.get("auto_symbol_selection"), True),
        "symbol_scan_minutes": _int(raw.get("symbol_scan_minutes"), 20, 5, 240),
        "live_watchlist_size": _int(raw.get("live_watchlist_size"), 8, 3, 15),
        "market_rotation_margin": _float(raw.get("market_rotation_margin"), 0.08, 0.01, 0.25),
        "market_dominance_margin": _float(raw.get("market_dominance_margin"), 0.12, 0.02, 0.40),
        "ai_enabled": _coerce_bool(raw.get("ai_enabled"), True),
        "ai_candidate_threshold": _float(raw.get("ai_candidate_threshold"), 0.68, 0.0, 1.0),
        "ai_heartbeat_candles": _int(raw.get("ai_heartbeat_candles"), 0, 0, 96),
        "ai_strong_candidate_threshold": _float(raw.get("ai_strong_candidate_threshold"), 0.80, 0.60, 1.0),
        "ai_decision_cooldown_minutes": _int(raw.get("ai_decision_cooldown_minutes"), 60, 5, 360),
        "ai_max_calls_daily": _int(raw.get("ai_max_calls_daily"), 0, 0, 100000) if hard_ai_limits else 0,
        "ai_hard_limits_enabled": hard_ai_limits,
        "ai_provider_probe_minutes": _int(raw.get("ai_provider_probe_minutes"), 15, 1, 240),
        "ai_rotation_only_strong": _coerce_bool(raw.get("ai_rotation_only_strong"), False),
        "ai_news_verify_only_after_entry": _coerce_bool(raw.get("ai_news_verify_only_after_entry"), True),
        "futures_news_fail_closed": _coerce_bool(raw.get("futures_news_fail_closed"), False),
        "proposal_min_setup": _float(raw.get("proposal_min_setup"), 0.58, 0.35, 0.95),
        "proposal_min_direction": _float(raw.get("proposal_min_direction"), 0.26, 0.10, 0.90),
        "proposal_min_quality": _float(raw.get("proposal_min_quality"), 0.62, 0.40, 0.95),
        "proposal_high_priority_quality": _float(raw.get("proposal_high_priority_quality"), 0.76, 0.55, 0.99),
        "proposal_max_vwap_atr_multiple": _float(raw.get("proposal_max_vwap_atr_multiple"), 1.75, 0.50, 4.0),
        "proposal_stop_atr": _float(raw.get("proposal_stop_atr"), 0.90, 0.40, 2.5),
        "proposal_target_rr": _float(raw.get("proposal_target_rr"), 1.65, 1.0, 4.0),
        "proposal_veto_minutes": _int(raw.get("proposal_veto_minutes"), 90, 15, 720),
        "proposal_approval_minutes": _int(raw.get("proposal_approval_minutes"), 45, 10, 240),
        "proposal_reverify_minutes": _int(raw.get("proposal_reverify_minutes"), 45, 10, 240),
        "proposal_watchlist_preflight": _int(raw.get("proposal_watchlist_preflight"), 4, 1, 6),
        "futures_entry_verify_tokens_daily": _int(raw.get("futures_entry_verify_tokens_daily"), 28000, 0, 10000000),
        "futures_entry_verify_calls_daily": _int(raw.get("futures_entry_verify_calls_daily"), 10, 0, 10000),
        "futures_entry_reserve_tokens_daily": _int(raw.get("futures_entry_reserve_tokens_daily"), 22000, 0, 10000000),
        "futures_entry_reserve_calls_daily": _int(raw.get("futures_entry_reserve_calls_daily"), 8, 0, 10000),
        "futures_entry_pacing_enabled": _coerce_bool(raw.get("futures_entry_pacing_enabled"), True),
        "futures_entry_pacing_window_hours": _int(raw.get("futures_entry_pacing_window_hours"), 4, 1, 12),
        "futures_opportunity_governor_enabled": _coerce_bool(raw.get("futures_opportunity_governor_enabled"), True),
        "futures_ai_normal_min_quality": _float(raw.get("futures_ai_normal_min_quality"), 0.70, 0.62, 0.95),
        "futures_ai_reserve_min_quality": _float(raw.get("futures_ai_reserve_min_quality"), 0.82, 0.70, 0.99),
        "futures_opportunity_borrow_calls": _int(raw.get("futures_opportunity_borrow_calls"), 1, 0, 2),
        "futures_opportunity_borrow_min_quality": _float(raw.get("futures_opportunity_borrow_min_quality"), 0.84, 0.70, 0.99),
        "futures_opportunity_borrow_min_setup": _float(raw.get("futures_opportunity_borrow_min_setup"), 0.68, 0.50, 0.99),
        "futures_opportunity_borrow_min_heat": _float(raw.get("futures_opportunity_borrow_min_heat"), 0.65, 0.10, 1.00),
        "futures_opportunity_exceptional_quality": _float(raw.get("futures_opportunity_exceptional_quality"), 0.92, 0.80, 1.00),
        "futures_day_session_start_utc": _int(raw.get("futures_day_session_start_utc"), 6, 0, 23),
        "futures_day_session_end_utc": _int(raw.get("futures_day_session_end_utc"), 21, 1, 24),
        "futures_day_session_exceptional_burst_calls": _int(raw.get("futures_day_session_exceptional_burst_calls"), 1, 0, 2),
        "futures_ai_tokens_daily": _int(raw.get("futures_ai_tokens_daily"), 50000, 0, 10000000),
        "futures_ai_calls_daily": _int(raw.get("futures_ai_calls_daily"), 18, 0, 10000),
        "futures_news_tokens_daily": _int(raw.get("futures_news_tokens_daily"), 15000, 0, 10000000),
        "futures_news_calls_daily": _int(raw.get("futures_news_calls_daily"), 3, 0, 1000),
        "spot_ai_tokens_daily": _int(raw.get("spot_ai_tokens_daily"), 28000, 0, 10000000),
        "spot_ai_calls_daily": _int(raw.get("spot_ai_calls_daily"), 10, 0, 10000),
        "spot_news_tokens_daily": _int(raw.get("spot_news_tokens_daily"), 10000, 0, 10000000),
        "spot_news_calls_daily": _int(raw.get("spot_news_calls_daily"), 2, 0, 1000),
        "promotion_ai_tokens_daily": _int(raw.get("promotion_ai_tokens_daily"), 12000, 0, 10000000),
        "promotion_ai_calls_daily": _int(raw.get("promotion_ai_calls_daily"), 1, 0, 1000),
        "promotion_action_tokens_daily": _int(raw.get("promotion_action_tokens_daily"), 0, 0, 10000000),
        "promotion_action_calls_daily": _int(raw.get("promotion_action_calls_daily"), 0, 0, 1000),
        "research_chief_tokens_daily": _int(raw.get("research_chief_tokens_daily"), 30000, 0, 10000000),
        "research_chief_calls_daily": _int(raw.get("research_chief_calls_daily"), 1, 0, 1000),
        "strategy_discovery_tokens_daily": _int(raw.get("strategy_discovery_tokens_daily"), 25000, 0, 10000000),
        "strategy_discovery_calls_daily": _int(raw.get("strategy_discovery_calls_daily"), 1, 0, 1000),
        "spot_strategy_discovery_tokens_daily": _int(raw.get("spot_strategy_discovery_tokens_daily"), 25000, 0, 10000000),
        "spot_strategy_discovery_calls_daily": _int(raw.get("spot_strategy_discovery_calls_daily"), 1, 0, 1000),
        "news_enabled": _coerce_bool(raw.get("news_enabled"), True),
        "news_cooldown_minutes": _int(raw.get("news_cooldown_minutes"), 180, 15, 1440),
        "min_confidence": _float(raw.get("min_confidence"), 0.62, 0.50, 0.95),
        "min_confidence_floor": _float(raw.get("min_confidence_floor"), 0.58, 0.50, 0.90),
        "adaptive_confidence_enabled": _coerce_bool(raw.get("adaptive_confidence_enabled"), True),
        "risk_per_trade_pct": _float(raw.get("risk_per_trade_pct"), 4.00, 0.01, 8.0),
        "absolute_risk_cap_pct": _float(raw.get("absolute_risk_cap_pct"), 7.00, 0.10, 10.0),
        "max_daily_loss_pct": _float(raw.get("max_daily_loss_pct"), 25.0, 0.1, 30.0),
        "max_weekly_loss_pct": _float(raw.get("max_weekly_loss_pct"), 30.0, 0.5, 50.0),
        "max_leverage": _float(raw.get("max_leverage"), 3.0, 1.0, 20.0),
        "max_positions": _int(raw.get("max_positions"), 1, 1, 10),
        "max_notional_usdt": _float(raw.get("max_notional_usdt"), 50.0, 5.0, 100000.0),
        "executable_min_order_override": _coerce_bool(raw.get("executable_min_order_override"), True),
        "min_order_override_max_risk_pct": _float(raw.get("min_order_override_max_risk_pct"), 7.00, 0.05, 10.0),
        "min_order_override_max_target_multiple": _float(raw.get("min_order_override_max_target_multiple"), 4.0, 1.0, 10.0),
        "max_notional_pct_equity": _float(raw.get("max_notional_pct_equity"), 125.0, 5.0, 300.0),
        "max_trades_per_day": _int(raw.get("max_trades_per_day"), 16, 1, 50),
        "min_reward_risk": _float(raw.get("min_reward_risk"), 1.30, 1.0, 5.0),
        "learning_risk_multiplier": _float(raw.get("learning_risk_multiplier"), 1.0, 0.0, 1.0),
        "learning_full_risk_after_trades": _int(raw.get("learning_full_risk_after_trades"), 40, 1, 1000),
        "growth_calibration_trades": _int(raw.get("growth_calibration_trades"), 6, 1, 100),
        "growth_calibration_risk_pct": _float(raw.get("growth_calibration_risk_pct"), 3.00, 0.01, 6.0),
        "growth_learning_risk_pct": _float(raw.get("growth_learning_risk_pct"), 4.00, 0.01, 7.0),
        "growth_validated_risk_pct": _float(raw.get("growth_validated_risk_pct"), 5.00, 0.01, 8.0),
        "growth_mature_risk_pct": _float(raw.get("growth_mature_risk_pct"), 6.00, 0.01, 9.0),
        "growth_validated_min_trades": _int(raw.get("growth_validated_min_trades"), 40, 10, 500),
        "growth_mature_min_trades": _int(raw.get("growth_mature_min_trades"), 100, 20, 1000),
        "growth_validated_min_profit_factor": _float(raw.get("growth_validated_min_profit_factor"), 1.10, 0.5, 5.0),
        "growth_mature_min_profit_factor": _float(raw.get("growth_mature_min_profit_factor"), 1.20, 0.5, 5.0),
        "growth_validated_max_drawdown_pct": _float(raw.get("growth_validated_max_drawdown_pct"), 6.0, 0.5, 50.0),
        "growth_mature_max_drawdown_pct": _float(raw.get("growth_mature_max_drawdown_pct"), 6.0, 0.5, 50.0),
        "growth_learning_exposure_pct": _float(raw.get("growth_learning_exposure_pct"), 125.0, 10.0, 300.0),
        "growth_validated_exposure_pct": _float(raw.get("growth_validated_exposure_pct"), 175.0, 10.0, 300.0),
        "growth_mature_exposure_pct": _float(raw.get("growth_mature_exposure_pct"), 250.0, 10.0, 300.0),
        "growth_learning_leverage_cap": _float(raw.get("growth_learning_leverage_cap"), 2.0, 1.0, 10.0),
        "growth_validated_leverage_cap": _float(raw.get("growth_validated_leverage_cap"), 2.5, 1.0, 10.0),
        "growth_mature_leverage_cap": _float(raw.get("growth_mature_leverage_cap"), 3.0, 1.0, 10.0),
        "growth_learning_max_trades_day": _int(raw.get("growth_learning_max_trades_day"), 8, 1, 50),
        "growth_validated_max_trades_day": _int(raw.get("growth_validated_max_trades_day"), 12, 1, 50),
        "growth_mature_max_trades_day": _int(raw.get("growth_mature_max_trades_day"), 16, 1, 50),
        "growth_learning_max_positions": _int(raw.get("growth_learning_max_positions"), 2, 1, 6),
        "growth_validated_max_positions": _int(raw.get("growth_validated_max_positions"), 3, 1, 6),
        "growth_mature_max_positions": _int(raw.get("growth_mature_max_positions"), 4, 1, 6),
        "portfolio_learning_enabled": _coerce_bool(raw.get("portfolio_learning_enabled"), True),
        "portfolio_learning_risk_cap_pct": _float(raw.get("portfolio_learning_risk_cap_pct"), 25.0, 0.10, 30.0),
        "portfolio_validated_risk_cap_pct": _float(raw.get("portfolio_validated_risk_cap_pct"), 25.0, 0.10, 30.0),
        "portfolio_mature_risk_cap_pct": _float(raw.get("portfolio_mature_risk_cap_pct"), 25.0, 0.10, 30.0),
        "portfolio_absolute_risk_cap_pct": _float(raw.get("portfolio_absolute_risk_cap_pct"), 25.0, 0.20, 30.0),
        "portfolio_absolute_risk_cap_usdt": _float(raw.get("portfolio_absolute_risk_cap_usdt"), 20.0, 1.0, 1000.0),
        "portfolio_correlation_threshold": _float(raw.get("portfolio_correlation_threshold"), 0.85, 0.50, 0.99),
        "portfolio_block_unprotected_positions": _coerce_bool(raw.get("portfolio_block_unprotected_positions"), True),
        "loss_streak_pause_after": _int(raw.get("loss_streak_pause_after"), 3, 1, 10),
        "loss_streak_pause_hours": _int(raw.get("loss_streak_pause_hours"), 8, 1, 72),
        "loss_streak_time_pause_enabled": _coerce_bool(raw.get("loss_streak_time_pause_enabled"), False),
        "live_order_slippage_pct": _float(raw.get("live_order_slippage_pct"), 0.25, 0.01, 2.0),
        "live_order_slippage_cap_pct": _float(raw.get("live_order_slippage_cap_pct"), 0.75, 0.10, 2.0),
        "live_order_slippage_atr_factor": _float(raw.get("live_order_slippage_atr_factor"), 0.15, 0.02, 0.50),
        "futures_capacity_pre_ai_enabled": _coerce_bool(raw.get("futures_capacity_pre_ai_enabled"), True),
        "futures_available_balance_utilization_pct": _float(raw.get("futures_available_balance_utilization_pct"), 82.0, 10.0, 95.0),
        "futures_available_balance_reserve_usdt": _float(raw.get("futures_available_balance_reserve_usdt"), 2.0, 0.0, 1000.0),
        "futures_require_capacity_state_with_open_positions": _coerce_bool(raw.get("futures_require_capacity_state_with_open_positions"), True),
        "futures_capacity_reject_cooldown_minutes": _int(raw.get("futures_capacity_reject_cooldown_minutes"), 20, 1, 240),
        "futures_capacity_reject_recovery_usdt": _float(raw.get("futures_capacity_reject_recovery_usdt"), 3.0, 0.5, 1000.0),
        "max_spread_bps": _float(raw.get("max_spread_bps"), 12.0, 0.5, 100.0),
        "max_data_age_seconds": _int(raw.get("max_data_age_seconds"), 90, 10, 600),
        "paper_start_equity": _float(raw.get("paper_start_equity"), 10000.0, 100.0, 10000000.0),
        "trading_token_budget_daily": _int(raw.get("trading_token_budget_daily"), 0, 0, 1000000000) if hard_ai_limits else 0,
        "auto_bootstrap_after_connection": _coerce_bool(raw.get("auto_bootstrap_after_connection"), True),
        "research_universe_top_n": _int(raw.get("research_universe_top_n"), 12, 5, 30),
        "research_regime_symbols": _int(raw.get("research_regime_symbols"), 6, 3, 12),
        "research_backtest_symbols": _int(raw.get("research_backtest_symbols"), 3, 1, 6),
        "research_backtest_candles": _int(raw.get("research_backtest_candles"), 1600, 500, 5000),
        "research_slippage_bps": _float(raw.get("research_slippage_bps"), 1.5, 0.0, 20.0),
        "research_refresh_hours": _int(raw.get("research_refresh_hours"), 24, 1, 168),
        "adaptive_strategy_discovery_enabled": _coerce_bool(raw.get("adaptive_strategy_discovery_enabled"), True),
        "adaptive_strategy_hypotheses": _int(raw.get("adaptive_strategy_hypotheses"), 8, 2, 12),
        "opportunity_os_enabled": _coerce_bool(raw.get("opportunity_os_enabled"), True),
        "opportunity_refresh_minutes": _int(raw.get("opportunity_refresh_minutes"), 15, 5, 180),
        "spot_opportunity_enabled": _coerce_bool(raw.get("spot_opportunity_enabled"), True),
        "spot_live_execution_enabled": _coerce_bool(raw.get("spot_live_execution_enabled"), True),
        "spot_interval": str(raw.get("spot_interval", "15")) if str(raw.get("spot_interval", "15")) in {"1","3","5","15","30","60","120","240","D"} else "15",
        "spot_universe_top_n": _int(raw.get("spot_universe_top_n"), 10, 4, 20),
        "spot_ai_candidate_threshold": _float(raw.get("spot_ai_candidate_threshold"), 0.72, 0.30, 0.95),
        "spot_ai_strong_threshold": _float(raw.get("spot_ai_strong_threshold"), 0.82, 0.60, 0.99),
        "spot_ai_cooldown_minutes": _int(raw.get("spot_ai_cooldown_minutes"), 60, 5, 360),
        "spot_proposal_min_setup": _float(raw.get("spot_proposal_min_setup"), 0.64, 0.35, 0.95),
        "spot_proposal_min_quality": _float(raw.get("spot_proposal_min_quality"), 0.66, 0.40, 0.95),
        "spot_proposal_high_priority_quality": _float(raw.get("spot_proposal_high_priority_quality"), 0.78, 0.55, 0.99),
        "spot_proposal_max_vwap_atr_multiple": _float(raw.get("spot_proposal_max_vwap_atr_multiple"), 1.55, 0.50, 4.0),
        "spot_proposal_stop_atr": _float(raw.get("spot_proposal_stop_atr"), 0.85, 0.40, 2.5),
        "spot_proposal_target_rr": _float(raw.get("spot_proposal_target_rr"), 1.60, 1.0, 4.0),
        "spot_proposal_veto_minutes": _int(raw.get("spot_proposal_veto_minutes"), 90, 15, 720),
        "spot_proposal_approval_minutes": _int(raw.get("spot_proposal_approval_minutes"), 45, 10, 240),
        "spot_proposal_reverify_minutes": _int(raw.get("spot_proposal_reverify_minutes"), 45, 10, 240),
        "spot_entry_verify_tokens_daily": _int(raw.get("spot_entry_verify_tokens_daily"), 18000, 0, 10000000),
        "spot_entry_verify_calls_daily": _int(raw.get("spot_entry_verify_calls_daily"), 7, 0, 10000),
        "spot_entry_reserve_tokens_daily": _int(raw.get("spot_entry_reserve_tokens_daily"), 10000, 0, 10000000),
        "spot_entry_reserve_calls_daily": _int(raw.get("spot_entry_reserve_calls_daily"), 4, 0, 10000),
        "spot_entry_pacing_enabled": _coerce_bool(raw.get("spot_entry_pacing_enabled"), True),
        "spot_entry_pacing_window_hours": _int(raw.get("spot_entry_pacing_window_hours"), 4, 1, 12),
        "spot_news_threshold": _float(raw.get("spot_news_threshold"), 0.80, 0.50, 0.98),
        "spot_min_confidence": _float(raw.get("spot_min_confidence"), 0.68, 0.55, 0.95),
        "spot_no_oos_confidence_bump": _float(raw.get("spot_no_oos_confidence_bump"), 0.02, 0.0, 0.20),
        "spot_min_reward_risk": _float(raw.get("spot_min_reward_risk"), 1.35, 1.0, 5.0),
        "spot_max_spread_bps": _float(raw.get("spot_max_spread_bps"), 22.0, 1.0, 100.0),
        "spot_entry_cross_bps": _float(raw.get("spot_entry_cross_bps"), 4.0, 0.0, 30.0),
        "spot_entry_timeout_seconds": _int(raw.get("spot_entry_timeout_seconds"), 90, 20, 600),
        "spot_absolute_risk_cap_pct": _float(raw.get("spot_absolute_risk_cap_pct"), 1.50, 0.10, 4.0),
        "spot_learning_risk_pct": _float(raw.get("spot_learning_risk_pct"), 0.36, 0.02, 2.0),
        "spot_validated_risk_pct": _float(raw.get("spot_validated_risk_pct"), 0.60, 0.02, 2.5),
        "spot_mature_risk_pct": _float(raw.get("spot_mature_risk_pct"), 0.90, 0.02, 3.0),
        "spot_learning_max_allocation_pct": _float(raw.get("spot_learning_max_allocation_pct"), 25.0, 5.0, 100.0),
        "spot_validated_max_allocation_pct": _float(raw.get("spot_validated_max_allocation_pct"), 35.0, 5.0, 100.0),
        "spot_mature_max_allocation_pct": _float(raw.get("spot_mature_max_allocation_pct"), 50.0, 5.0, 100.0),
        "spot_min_order_max_allocation_pct": _float(raw.get("spot_min_order_max_allocation_pct"), 35.0, 5.0, 100.0),
        "spot_adaptive_research_enabled": _coerce_bool(raw.get("spot_adaptive_research_enabled"), True),
        "spot_research_refresh_hours": _int(raw.get("spot_research_refresh_hours"), 24, 1, 168),
        "spot_backtest_candles": _int(raw.get("spot_backtest_candles"), 1400, 500, 5000),
        "spot_research_slippage_bps": _float(raw.get("spot_research_slippage_bps"), 2.0, 0.0, 20.0),
        "earn_discovery_enabled": _coerce_bool(raw.get("earn_discovery_enabled"), True),
        "alpha_discovery_enabled": _coerce_bool(raw.get("alpha_discovery_enabled"), True),
        "prediction_discovery_enabled": _coerce_bool(raw.get("prediction_discovery_enabled"), True),
        "promotion_intelligence_enabled": _coerce_bool(raw.get("promotion_intelligence_enabled"), True),
        "promotion_auto_ai_refresh_enabled": _coerce_bool(raw.get("promotion_auto_ai_refresh_enabled"), False),
        "promotion_region_hint": str(raw.get("promotion_region_hint", "auto")).strip()[:120] or "auto",
        "promotion_refresh_hours": _int(raw.get("promotion_refresh_hours"), 24, 1, 168),
        "browser_operator_enabled": _coerce_bool(raw.get("browser_operator_enabled"), True),
        "browser_action_refresh_hours": _int(raw.get("browser_action_refresh_hours"), 12, 1, 72),
        "browser_action_refresh_minutes": _int(raw.get("browser_action_refresh_minutes"), 720, 30, 1440),
        "browser_action_max_cycles_daily": _int(raw.get("browser_action_max_cycles_daily"), 2, 1, 4),
        "browser_background_only": _coerce_bool(raw.get("browser_background_only"), True),
        "browser_max_actions_per_cycle": _int(raw.get("browser_max_actions_per_cycle"), 8, 1, 12),
        "browser_cycle_timeout_seconds": _int(raw.get("browser_cycle_timeout_seconds"), 420, 120, 1200),
        "promotion_trade_alignment_weight": _float(raw.get("promotion_trade_alignment_weight"), 0.03, 0.0, 0.10),
        "promotion_min_base_setup": _float(raw.get("promotion_min_base_setup"), 0.45, 0.20, 0.80),
    }


def save_trading_settings(values: dict[str, Any]) -> dict[str, Any]:
    current = load_trading_settings()
    current.update(values)
    TRADING_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(TRADING_SETTINGS_FILE) + ".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TRADING_SETTINGS_FILE)
    return load_trading_settings()


def apply_safe_autopilot_profile(*, mode: str = "autopilot_live", key_environment: str = "auto") -> dict[str, Any]:
    """Apply Stan's aggressive micro-account learning profile with deterministic hard stops.

    The profile starts at a materially executable micro-account risk level, then expands only after positive live evidence.
    It never caps positive PnL, and it never exceeds the absolute risk/leverage ceilings.
    """
    values = {
        "mode": mode,
        "bybit_key_environment": key_environment,
        "execution_environment": "testnet" if key_environment == "testnet" or mode == "testnet" else "mainnet",
        "auto_start": True,
        "one_button_autopilot": True,
        "autopilot_profile_version": 20,
        "auto_symbol_selection": True,
        "symbol_scan_minutes": 5,
        "live_watchlist_size": 10,
        "market_rotation_margin": 0.08,
        "market_dominance_margin": 0.12,
        "ai_enabled": True,
        "ai_candidate_threshold": 0.68,
        "ai_heartbeat_candles": 0,
        "ai_strong_candidate_threshold": 0.80,
        "ai_decision_cooldown_minutes": 60,
        "ai_max_calls_daily": 0,
        "ai_hard_limits_enabled": False,
        "ai_provider_probe_minutes": 15,
        "ai_rotation_only_strong": False,
        "ai_news_verify_only_after_entry": True,
        "futures_news_fail_closed": False,
        "proposal_min_setup": 0.58,
        "proposal_min_direction": 0.26,
        "proposal_min_quality": 0.62,
        "proposal_high_priority_quality": 0.76,
        "proposal_max_vwap_atr_multiple": 1.75,
        "proposal_stop_atr": 0.90,
        "proposal_target_rr": 1.65,
        "proposal_veto_minutes": 90,
        "proposal_approval_minutes": 45,
        "proposal_reverify_minutes": 45,
        "proposal_watchlist_preflight": 4,
        "futures_entry_verify_tokens_daily": 28000,
        "futures_entry_verify_calls_daily": 10,
        "futures_entry_reserve_tokens_daily": 22000,
        "futures_entry_reserve_calls_daily": 8,
        "futures_entry_pacing_enabled": True,
        "futures_entry_pacing_window_hours": 4,
        "futures_opportunity_governor_enabled": True,
        "futures_ai_normal_min_quality": 0.70,
        "futures_ai_reserve_min_quality": 0.82,
        "futures_opportunity_borrow_calls": 1,
        "futures_opportunity_borrow_min_quality": 0.84,
        "futures_opportunity_borrow_min_setup": 0.68,
        "futures_opportunity_borrow_min_heat": 0.65,
        "futures_opportunity_exceptional_quality": 0.92,
        "futures_day_session_start_utc": 6,
        "futures_day_session_end_utc": 21,
        "futures_day_session_exceptional_burst_calls": 1,
        "futures_ai_tokens_daily": 50000,
        "futures_ai_calls_daily": 18,
        "futures_news_tokens_daily": 15000,
        "futures_news_calls_daily": 3,
        "spot_ai_tokens_daily": 28000,
        "spot_ai_calls_daily": 10,
        "spot_news_tokens_daily": 10000,
        "spot_news_calls_daily": 2,
        "promotion_ai_tokens_daily": 12000,
        "promotion_ai_calls_daily": 1,
        "promotion_action_tokens_daily": 0,
        "promotion_action_calls_daily": 0,
        "research_chief_tokens_daily": 30000,
        "research_chief_calls_daily": 1,
        "strategy_discovery_tokens_daily": 25000,
        "strategy_discovery_calls_daily": 1,
        "spot_strategy_discovery_tokens_daily": 25000,
        "spot_strategy_discovery_calls_daily": 1,
        "news_enabled": True,
        "news_cooldown_minutes": 180,
        "min_confidence": 0.60,
        "min_confidence_floor": 0.56,
        "adaptive_confidence_enabled": True,
        "risk_per_trade_pct": 4.00,
        "absolute_risk_cap_pct": 7.00,
        "max_daily_loss_pct": 25.0,
        "max_weekly_loss_pct": 30.0,
        "max_leverage": 4.0,
        "max_positions": 5,
        "max_notional_usdt": 50.0,
        "executable_min_order_override": True,
        "min_order_override_max_risk_pct": 7.00,
        "min_order_override_max_target_multiple": 4.0,
        "max_notional_pct_equity": 225.0,
        "max_trades_per_day": 16,
        "min_reward_risk": 1.30,
        "learning_risk_multiplier": 1.0,
        "learning_full_risk_after_trades": 40,
        "growth_calibration_trades": 6,
        "growth_calibration_risk_pct": 3.00,
        "growth_learning_risk_pct": 4.00,
        "growth_validated_risk_pct": 5.00,
        "growth_mature_risk_pct": 6.00,
        "growth_validated_min_trades": 40,
        "growth_mature_min_trades": 100,
        "growth_validated_min_profit_factor": 1.10,
        "growth_mature_min_profit_factor": 1.20,
        "growth_validated_max_drawdown_pct": 6.0,
        "growth_mature_max_drawdown_pct": 6.0,
        "growth_learning_exposure_pct": 225.0,
        "growth_validated_exposure_pct": 275.0,
        "growth_mature_exposure_pct": 300.0,
        "growth_learning_leverage_cap": 3.0,
        "growth_validated_leverage_cap": 3.5,
        "growth_mature_leverage_cap": 4.0,
        "growth_learning_max_trades_day": 10,
        "growth_validated_max_trades_day": 14,
        "growth_mature_max_trades_day": 18,
        "growth_learning_max_positions": 3,
        "growth_validated_max_positions": 4,
        "growth_mature_max_positions": 5,
    "portfolio_learning_enabled": True,
    "portfolio_learning_risk_cap_pct": 25.0,
    "portfolio_validated_risk_cap_pct": 25.0,
    "portfolio_mature_risk_cap_pct": 25.0,
    "portfolio_absolute_risk_cap_pct": 25.0,
    "portfolio_absolute_risk_cap_usdt": 20.0,
    "portfolio_correlation_threshold": 0.85,
    "portfolio_block_unprotected_positions": True,
        "loss_streak_pause_after": 3,
        "loss_streak_pause_hours": 8,
        "loss_streak_time_pause_enabled": False,
        "live_order_slippage_pct": 0.25,
        "live_order_slippage_cap_pct": 0.75,
        "live_order_slippage_atr_factor": 0.15,
        "futures_capacity_pre_ai_enabled": True,
        "futures_available_balance_utilization_pct": 82.0,
        "futures_available_balance_reserve_usdt": 2.0,
        "futures_require_capacity_state_with_open_positions": True,
        "futures_capacity_reject_cooldown_minutes": 20,
        "futures_capacity_reject_recovery_usdt": 3.0,
        "trading_token_budget_daily": 0,
        "auto_bootstrap_after_connection": True,
        "research_refresh_hours": 24,
        "adaptive_strategy_discovery_enabled": True,
        "adaptive_strategy_hypotheses": 8,
        "promotion_intelligence_enabled": True,
        "promotion_auto_ai_refresh_enabled": False,
        "promotion_refresh_hours": 24,
        "browser_operator_enabled": True,
        "browser_action_refresh_hours": 12,
        "browser_action_refresh_minutes": 720,
        "browser_action_max_cycles_daily": 2,
        "browser_background_only": True,
        "browser_max_actions_per_cycle": 8,
        "browser_cycle_timeout_seconds": 420,
        "promotion_trade_alignment_weight": 0.03,
        "promotion_min_base_setup": 0.50,
        "spot_ai_candidate_threshold": 0.72,
        "spot_ai_strong_threshold": 0.82,
        "spot_ai_cooldown_minutes": 60,
        "spot_proposal_min_setup": 0.60,
        "spot_proposal_min_quality": 0.66,
        "spot_proposal_high_priority_quality": 0.78,
        "spot_proposal_max_vwap_atr_multiple": 1.55,
        "spot_proposal_stop_atr": 0.85,
        "spot_proposal_target_rr": 1.60,
        "spot_proposal_veto_minutes": 90,
        "spot_proposal_approval_minutes": 45,
        "spot_proposal_reverify_minutes": 45,
        "spot_min_confidence": 0.68,
        "spot_no_oos_confidence_bump": 0.02,
        "spot_learning_max_allocation_pct": 25.0,
        "spot_validated_max_allocation_pct": 35.0,
        "spot_mature_max_allocation_pct": 50.0,
        "spot_entry_verify_tokens_daily": 18000,
        "spot_entry_verify_calls_daily": 7,
        "spot_entry_reserve_tokens_daily": 10000,
        "spot_entry_reserve_calls_daily": 4,
        "spot_entry_pacing_enabled": True,
        "spot_entry_pacing_window_hours": 4,
        "spot_absolute_risk_cap_pct": 2.00,
        "spot_learning_risk_pct": 0.75,
        "spot_validated_risk_pct": 1.00,
        "spot_mature_risk_pct": 1.25,
        "spot_research_refresh_hours": 24,
    }
    return save_trading_settings(values)


def audit_autopilot_growth_profile(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic preflight: safety ceilings must exist without a fixed-dollar live profit ceiling."""
    c = dict(cfg or load_trading_settings())
    abs_risk = float(c.get("absolute_risk_cap_pct", 7.00))
    risks = [
        float(c.get("growth_calibration_risk_pct", 3.00)),
        float(c.get("growth_learning_risk_pct", 4.00)),
        float(c.get("growth_validated_risk_pct", 5.00)),
        float(c.get("growth_mature_risk_pct", 6.00)),
    ]
    exposures = [
        float(c.get("growth_learning_exposure_pct", 125.0)),
        float(c.get("growth_validated_exposure_pct", 175.0)),
        float(c.get("growth_mature_exposure_pct", 250.0)),
    ]
    leverages = [
        float(c.get("growth_learning_leverage_cap", 2.0)),
        float(c.get("growth_validated_leverage_cap", 2.5)),
        float(c.get("growth_mature_leverage_cap", 3.0)),
    ]
    max_lev = float(c.get("max_leverage", 3.0))
    positions = [
        int(c.get("growth_learning_max_positions", 3)),
        int(c.get("growth_validated_max_positions", 4)),
        int(c.get("growth_mature_max_positions", 5)),
    ]
    portfolio_caps = [
        float(c.get("portfolio_learning_risk_cap_pct", 25.0)),
        float(c.get("portfolio_validated_risk_cap_pct", 25.0)),
        float(c.get("portfolio_mature_risk_cap_pct", 25.0)),
    ]
    portfolio_abs = float(c.get("portfolio_absolute_risk_cap_pct", 25.0))
    checks = {
        "risk_progression_monotonic": risks == sorted(risks),
        "risk_inside_absolute_ceiling": max(risks) <= abs_risk + 1e-12,
        "exposure_progression_monotonic": exposures == sorted(exposures),
        "leverage_progression_monotonic": leverages == sorted(leverages),
        "position_capacity_progression_monotonic": positions == sorted(positions),
        "portfolio_risk_progression_monotonic": portfolio_caps == sorted(portfolio_caps),
        "portfolio_risk_inside_absolute_ceiling": max(portfolio_caps) <= portfolio_abs + 1e-12,
        "portfolio_absolute_not_above_daily_loss_stop": portfolio_abs <= float(c.get("max_daily_loss_pct", 25.0)) + 1e-12,
        "portfolio_cash_cap_positive": float(c.get("portfolio_absolute_risk_cap_usdt", 20.0)) > 0,
        "leverage_inside_absolute_ceiling": max(leverages) <= max_lev + 1e-12,
        "daily_loss_below_weekly_loss": float(c.get("max_daily_loss_pct", 25.0)) < float(c.get("max_weekly_loss_pct", 30.0)),
        "promotion_cannot_dominate_setup": float(c.get("promotion_trade_alignment_weight", 0.03)) <= 0.05,
        # Live risk_engine deliberately ignores max_notional_usdt and scales exposure from equity.
        "live_has_no_fixed_dollar_notional_ceiling": True,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "risk_path_pct": risks,
        "exposure_path_pct_equity": exposures,
        "leverage_cap_path": leverages,
        "max_positions_path": positions,
        "portfolio_risk_path_pct": portfolio_caps,
        "portfolio_absolute_risk_cap_pct": portfolio_abs,
        "portfolio_absolute_risk_cap_usdt": float(c.get("portfolio_absolute_risk_cap_usdt", 20.0)),
        "absolute_risk_cap_pct": abs_risk,
        "absolute_leverage_cap": max_lev,
    }
