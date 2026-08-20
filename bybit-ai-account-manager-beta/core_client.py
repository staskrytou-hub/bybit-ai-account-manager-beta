from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import request

from paths import DATA_DIR
from runtime_control import clear_manual_stop, manual_stop_active, manual_stop_info, request_manual_stop
from trading_config import has_bybit_credentials, load_trading_settings

HOST = "127.0.0.1"
PORT = 8767
BASE = f"http://{HOST}:{PORT}"
TOKEN_FILE = DATA_DIR / "stan_core.token"


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _req(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 3.0) -> Any:
    data = json.dumps(payload or {}).encode("utf-8") if method != "GET" else None
    headers = {"Content-Type": "application/json"}
    token = _token()
    if token:
        headers["X-Stan-Core-Token"] = token
    req = request.Request(BASE + path, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def health() -> bool:
    try:
        return bool(_req("/health", timeout=0.8).get("ok"))
    except Exception:
        return False


def ensure_running(wait_seconds: float = 5.0, *, explicit_start: bool = False) -> bool:
    """Start StanCore only when allowed.

    A user manual STOP is a persistent latch. Background status polling and Windows autostart
    are not allowed to clear it. Only an explicit START STAN call may clear the latch first.
    """
    if health():
        return True
    if manual_stop_active() and not explicit_start:
        return False

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve().parent / "StanCore.exe"
        if not exe.exists():
            return False
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [str(exe)], cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=creationflags, close_fds=(os.name != "nt"),
        )
    else:
        script = Path(__file__).resolve().parent / "core_main.py"
        subprocess.Popen(
            [sys.executable, str(script)], cwd=str(script.parent), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=(os.name != "nt"),
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if health():
            return True
        time.sleep(0.25)
    return False



def _force_kill_core_process() -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/IM", "StanCore.exe", "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=4, check=False,
        )
    except Exception:
        pass

def status() -> dict[str, Any]:
    # IMPORTANT: status polling must never restart a manually stopped Core.
    if health():
        try:
            result = dict(_req("/status", timeout=4.0))
            result["manual_stop"] = manual_stop_info()
            return result
        except Exception as exc:
            return {"running": False, "last_error": f"{type(exc).__name__}: {exc}", "manual_stop": manual_stop_info()}

    if manual_stop_active():
        return {
            "running": False,
            "trading": {"running": False, "state": "manual_stop", "message": "Stopped by user"},
            "manual_stop": manual_stop_info(),
            "bybit_credentials_configured": has_bybit_credentials(),
            "settings": load_trading_settings(),
            "message": "STOPPED MANUALLY — press START STAN to run again",
        }

    if not ensure_running():
        return {"running": False, "last_error": "Stan Core is not running", "manual_stop": manual_stop_info()}
    try:
        result = dict(_req("/status", timeout=4.0))
        result["manual_stop"] = manual_stop_info()
        return result
    except Exception as exc:
        return {"running": False, "last_error": f"{type(exc).__name__}: {exc}", "manual_stop": manual_stop_info()}


def events() -> list[dict[str, Any]]:
    if manual_stop_active() and not health():
        return []
    try:
        return list((_req("/events", timeout=4.0) or {}).get("events", []))
    except Exception:
        return []


def command(action: str, timeout: float = 30.0) -> Any:
    action = str(action or "")

    if action == "hard_stop":
        # Latch locally FIRST, so even if Core dies before replying it cannot be resurrected by UI polling.
        latch = request_manual_stop("User pressed STOP in Stan Desktop")
        result: Any = {"accepted": True, "manual_stop": latch, "core_shutdown": True}
        if health():
            try:
                result = _req("/command", method="POST", payload={"action": "hard_stop"}, timeout=min(timeout, 8.0))
            except Exception:
                # The process may terminate before the HTTP reply completes. The local latch is authoritative.
                pass
        # Strict kill-switch: if the background executable did not exit promptly, terminate StanCore.exe.
        deadline = time.time() + 1.5
        while time.time() < deadline and health():
            time.sleep(0.1)
        if health():
            _force_kill_core_process()
        return result

    if action == "start_autopilot":
        # Explicit START is the ONLY path that clears the persistent manual-stop latch.
        clear_manual_stop()
        if not ensure_running(explicit_start=True):
            raise RuntimeError("Stan Core could not be started after clearing the manual STOP latch.")
        return _req("/command", method="POST", payload={"action": action}, timeout=timeout)

    if manual_stop_active():
        raise RuntimeError("Stan is manually stopped. Press START STAN before running any autonomous action.")
    if not ensure_running():
        raise RuntimeError("Stan Core is not running.")
    return _req("/command", method="POST", payload={"action": action}, timeout=timeout)
