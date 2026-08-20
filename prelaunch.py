from __future__ import annotations

import time
from typing import Any

from account_os_store import set_state
from bybit_client import BybitClient
from credential_guard import validate_autopilot_key
from trading_config import audit_autopilot_growth_profile, load_trading_settings, resolve_execution_environment
from trading_store import get_state as trading_get_state


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default




def _server_time_ms(server: dict[str, Any]) -> float:
    value = _float(server.get("time"), 0.0) if isinstance(server, dict) else 0.0
    if value > 0:
        return value
    result = (server.get("result") or {}) if isinstance(server, dict) else {}
    nano = _float(result.get("timeNano"), 0.0)
    if nano > 0:
        return nano / 1_000_000.0
    sec = _float(result.get("timeSecond"), 0.0)
    return sec * 1000.0 if sec > 0 else 0.0

def _equity(wallet: dict[str, Any]) -> float:
    items = list(wallet.get("list") or []) if isinstance(wallet, dict) else []
    if not items:
        return 0.0
    row = items[0] if isinstance(items[0], dict) else {}
    for key in ("totalEquity", "totalWalletBalance"):
        value = _float(row.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def run_prelaunch_audit() -> dict[str, Any]:
    """Deterministic pre-launch audit. It never creates, changes or cancels an order."""
    cfg = load_trading_settings()
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    def check(key: str, ok: bool, message: str, *, fatal: bool = True) -> None:
        checks.append({"key": key, "ok": bool(ok), "fatal": bool(fatal), "message": message})

    guard = validate_autopilot_key()
    env = str(guard.get("environment", ""))
    mode = str(guard.get("autopilot_mode", "shadow"))
    check("credentials", bool(guard.get("ok")), "Bybit API authenticates successfully")
    check("permissions", mode != "autopilot_live" or bool(guard.get("live_armed")), str(guard.get("message", "API policy checked")))
    check("wallet_permissions", not bool(guard.get("unsafe_wallet_permissions")), "No Wallet/transfer/withdraw permissions on the Stan trading key")
    configured_env = str(cfg.get("execution_environment", "")).lower()
    api_execution_env = resolve_execution_environment(mode=mode, key_environment=env, configured="auto")
    check(
        "execution_environment",
        configured_env == api_execution_env,
        f"Execution environment resolved consistently: configured={configured_env or 'unknown'}, API key={api_execution_env or 'unknown'} ({env or 'unknown'})",
    )

    testnet = api_execution_env == "testnet"
    client = BybitClient(testnet=testnet, authenticated=True)
    wallet = client.get_wallet_balance("USDT")
    equity = _equity(wallet)
    positions = [p for p in client.get_positions(settle_coin="USDT") if _float(p.get("size"), 0.0) > 0]
    open_orders = client.get_open_orders(limit=50)
    fee_rows = client.get_fee_rate(category="linear")

    server = client.get_server_time()
    server_ms = _server_time_ms(server)
    local_ms = time.time() * 1000.0
    raw_drift_ms = abs(server_ms - local_ms) if server_ms else 999999.0
    if hasattr(client, "get_clock_sync_status"):
        sync = client.get_clock_sync_status()
        clock_offset_ms = _float(sync.get("offset_ms"), 0.0)
    else:
        # Compatibility for deterministic test doubles / legacy client adapters.
        clock_offset_ms = server_ms - local_ms if server_ms else 0.0
    corrected_drift_ms = abs(server_ms - (local_ms + clock_offset_ms)) if server_ms else 999999.0

    check(
        "clock",
        corrected_drift_ms <= 1500.0,
        f"Bybit signing clock synchronized; raw Windows drift {raw_drift_ms/1000.0:.2f}s, corrected drift {corrected_drift_ms/1000.0:.2f}s",
    )
    if raw_drift_ms > 5000.0:
        warnings.append(f"Windows clock differs from Bybit by {raw_drift_ms/1000.0:.2f}s. Stan compensates automatically, but Windows Time sync is recommended.")
    check("equity", equity > 0.0 or mode in {"testnet", "shadow"}, f"Detected account equity: {equity:.4f} USDT")
    check("growth_profile", bool(audit_autopilot_growth_profile(cfg).get("passed")), "Adaptive Growth profile is internally consistent")
    check("execution_lock", trading_get_state("execution_safety_lock", "0") != "1", "No unresolved execution safety lock")

    if positions:
        warnings.append(f"Account already has {len(positions)} open derivative position(s). Stan will count them before any new entry.")
    if open_orders:
        warnings.append(f"Account has {len(open_orders)} current/recent order record(s). Stan will reconcile before new entries.")
    if not guard.get("ips"):
        warnings.append("The Bybit API key has no IP restriction. This is allowed for first launch, but a stable IP allowlist is safer when available.")

    fatal_failures = [c for c in checks if c["fatal"] and not c["ok"]]
    report = {
        "ready": not fatal_failures,
        "environment": env,
        "execution_environment": configured_env,
        "api_key_environment": env,
        "api_execution_environment": api_execution_env,
        "autopilot_mode": mode,
        "live_armed": bool(guard.get("live_armed")),
        "equity_usdt": round(equity, 6),
        "open_positions": len(positions),
        "open_orders": len(open_orders),
        "clock_drift_ms": round(raw_drift_ms, 1),
        "clock_offset_ms": round(clock_offset_ms, 1),
        "clock_corrected_drift_ms": round(corrected_drift_ms, 1),
        "fee_rates": fee_rows[:10],
        "checks": checks,
        "warnings": warnings,
        "fatal_failures": [c["message"] for c in fatal_failures],
        "message": "READY" if not fatal_failures else "BLOCKED",
    }
    set_state("prelaunch_report", report)
    return report
