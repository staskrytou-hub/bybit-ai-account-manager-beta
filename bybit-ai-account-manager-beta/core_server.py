from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from account_os import ACCOUNT_OS
from account_os_store import recent_events
from paths import DATA_DIR
from runtime_control import manual_stop_active, runtime_snapshot

HOST = "127.0.0.1"
PORT = 8767
TOKEN_FILE = DATA_DIR / "stan_core.token"


def _token() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        value=TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(value)>=24:
            return value
    value=secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(value,encoding="utf-8")
    return value

TOKEN=_token()


class Handler(BaseHTTPRequestHandler):
    server_version="StanCore/4.6.9"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _auth(self) -> bool:
        return self.headers.get("X-Stan-Core-Token", "") == TOKEN

    def _json(self, status: int, payload: Any) -> None:
        data=json.dumps(payload,ensure_ascii=False,default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200,{"ok":True,"service":"Stan Core","version":"4.6.9","manual_stop":manual_stop_active(),"runtime":runtime_snapshot()}); return
        if not self._auth(): self._json(401,{"error":"unauthorized"}); return
        if self.path == "/status": self._json(200,ACCOUNT_OS.status()); return
        if self.path.startswith("/events"):
            self._json(200,{"events":recent_events(120)}); return
        self._json(404,{"error":"not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._auth(): self._json(401,{"error":"unauthorized"}); return
        try:
            length=int(self.headers.get("Content-Length","0") or 0)
            body=json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            body={}
        action=str(body.get("action", ""))
        try:
            if action=="start_autopilot": result=ACCOUNT_OS.start_autopilot()
            elif action=="prelaunch": result=ACCOUNT_OS.prelaunch()
            elif action=="start_trading": ACCOUNT_OS.start_trading(); result={"accepted":True}
            elif action=="stop_trading": ACCOUNT_OS.stop_trading(); result={"accepted":True}
            elif action=="hard_stop":
                result=ACCOUNT_OS.hard_stop(); threading.Thread(target=self.server.shutdown,daemon=True).start()
            elif action=="analyze_now": result=ACCOUNT_OS.analyze_now()
            elif action=="run_bootstrap": result={"accepted":ACCOUNT_OS.run_bootstrap_async(force=True)}
            elif action=="refresh_promotions": result={"accepted":ACCOUNT_OS.refresh_promotions_async(force=True)}
            elif action=="refresh_opportunities": result={"accepted":ACCOUNT_OS.refresh_opportunities_async(force_research=True)}
            elif action=="authorize_bybit_browser": result=ACCOUNT_OS.authorize_bybit_browser()
            elif action=="shutdown":
                ACCOUNT_OS.stop(); result={"accepted":True}; threading.Thread(target=self.server.shutdown,daemon=True).start()
            else: self._json(400,{"error":"unknown_action","action":action}); return
            self._json(200,result)
        except Exception as exc:
            self._json(500,{"error":f"{type(exc).__name__}: {exc}"})


def run_server() -> None:
    if manual_stop_active():
        return
    ACCOUNT_OS.start()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    try: server.serve_forever(poll_interval=0.5)
    finally:
        ACCOUNT_OS.stop(); server.server_close()
