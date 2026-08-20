from __future__ import annotations

import os
import json
import socket
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib import request as urlrequest

from paths import DATA_DIR
from runtime_control import interruptible_wait, runtime_stop_requested

# Exact official Bybit properties used by promotions/rewards. Keep this narrow on purpose.
ALLOWED_HOSTS = {
    "bybit.com", "www.bybit.com", "announcements.bybit.com",
    "bybit.eu", "www.bybit.eu", "announcements.bybit.eu",
}
PROFILE_DIR = DATA_DIR / "bybit_browser_profile_v455"
SESSION_FILE = DATA_DIR / "bybit_browser_session_v455.json"

SAFE_ACTION_WORDS = ("register", "join", "claim", "spin", "check in", "check-in", "participate", "enroll")
DANGEROUS_ACTION_WORDS = (
    "deposit", "transfer", "withdraw", "buy", "purchase", "subscribe", "stake", "earn",
    "p2p", "refer", "referral", "card", "pay", "convert", "loan", "borrow", "margin loan",
)


def _official_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in ALLOWED_HOSTS
    except Exception:
        return False


def _path_from_command(command: str) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    match = re.search(r'"([^\"]+\.exe)"', text, flags=re.I)
    if not match:
        match = re.search(r'([^\s]+\.exe)', text, flags=re.I)
    if not match:
        return None
    raw = os.path.expandvars(match.group(1).strip())
    try:
        path = Path(raw)
        return str(path) if path.exists() else None
    except Exception:
        return None


def _browser_label(path: str) -> str:
    name = Path(path).name.lower()
    if name == "chrome.exe":
        return "Google Chrome"
    if name == "msedge.exe":
        return "Microsoft Edge"
    if name == "brave.exe":
        return "Brave"
    return "Chromium"


def _registry_app_path(exe_name: str) -> str | None:
    """Read Windows App Paths without pywin32."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    keys = (
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
    )
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, None)
            path = Path(os.path.expandvars(str(value).strip('"')))
            if path.exists():
                return str(path)
        except Exception:
            continue
    return None


def _default_browser_registry_path() -> str | None:
    """Resolve the Windows default HTTPS browser from UserChoice -> ProgId -> open command."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    progid = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            progid, _ = winreg.QueryValueEx(key, "ProgId")
    except Exception:
        return None
    progid = str(progid or "").strip()
    if not progid:
        return None
    candidates = (
        (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{progid}\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command"),
        (winreg.HKEY_LOCAL_MACHINE, rf"Software\Classes\{progid}\shell\open\command"),
    )
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                command, _ = winreg.QueryValueEx(key, None)
            resolved = _path_from_command(str(command))
            if resolved:
                return resolved
        except Exception:
            continue
    return None


def _browser_candidates() -> list[tuple[str, Path]]:
    pf = Path(os.environ.get("PROGRAMW6432") or os.environ.get("PROGRAMFILES") or r"C:\Program Files")
    pfx86 = Path(os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)")
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    user = Path(os.environ.get("USERPROFILE", ""))
    return [
        ("Google Chrome", pf / "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", pfx86 / "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", local / "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", user / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        ("Microsoft Edge", pf / "Microsoft/Edge/Application/msedge.exe"),
        ("Microsoft Edge", pfx86 / "Microsoft/Edge/Application/msedge.exe"),
        ("Microsoft Edge", local / "Microsoft/Edge/Application/msedge.exe"),
        ("Brave", pf / "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("Brave", pfx86 / "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("Brave", local / "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("Chromium", pf / "Chromium/Application/chrome.exe"),
        ("Chromium", pfx86 / "Chromium/Application/chrome.exe"),
        ("Chromium", local / "Chromium/Application/chrome.exe"),
    ]


def _find_browser(*, force_windows: bool | None = None) -> dict[str, str] | None:
    """Find an installed Chromium-family browser through registry, defaults, paths and PATH."""
    is_windows = os.name == "nt" if force_windows is None else bool(force_windows)
    if not is_windows:
        return None

    if force_windows is None:
        default_path = _default_browser_registry_path()
        if default_path:
            return {"name": _browser_label(default_path), "path": default_path, "source": "default_https"}

        for label, exe in (
            ("Google Chrome", "chrome.exe"),
            ("Microsoft Edge", "msedge.exe"),
            ("Brave", "brave.exe"),
        ):
            path = _registry_app_path(exe)
            if path:
                return {"name": label, "path": path, "source": "app_paths"}

    for label, path in _browser_candidates():
        try:
            if str(path) and path.exists():
                return {"name": label, "path": str(path), "source": "standard_path"}
        except Exception:
            continue

    for label, exe in (
        ("Google Chrome", "chrome.exe"),
        ("Microsoft Edge", "msedge.exe"),
        ("Brave", "brave.exe"),
        ("Chromium", "chromium.exe"),
    ):
        found = shutil.which(exe)
        if found:
            return {"name": label, "path": found, "source": "PATH"}
    return None



def _read_session() -> dict[str, Any]:
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_session(data: dict[str, Any]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(SESSION_FILE) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SESSION_FILE)


def _debug_endpoint_alive(port: int) -> bool:
    if int(port or 0) <= 0:
        return False
    try:
        with urlrequest.urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return bool(isinstance(data, dict) and data.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _free_debug_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch_authorization_browser(url: str = "https://www.bybit.com/en/rewards_hub") -> dict[str, Any]:
    """Open Stan's dedicated visible Chrome profile with a loopback CDP endpoint.

    Unlike v4.5.4, the browser may remain open after login. The Browser Operator connects to
    the same process over localhost CDP instead of trying to launch a second Chrome against a
    profile that is already locked. CAPTCHA/2FA remain manual and are never bypassed.
    """
    if runtime_stop_requested():
        return {"opened": False, "reason": "manual STOP active"}
    if not _official_url(url):
        raise ValueError("Authorization browser is restricted to official Bybit domains.")
    browser = _find_browser()
    if not browser or not browser.get("path"):
        return {"opened": False, "reason": "no supported browser found"}

    existing = _read_session()
    existing_port = int(existing.get("debug_port", 0) or 0)
    if _debug_endpoint_alive(existing_port):
        return {
            "opened": True,
            "already_running": True,
            "pid": int(existing.get("pid", 0) or 0),
            "browser": existing.get("browser") or browser.get("name", "Browser"),
            "url": url,
            "profile": str(PROFILE_DIR),
            "debug_port": existing_port,
            "state": "READY_OR_AUTHENTICATING",
            "instruction": "Use the visible Stan Bybit browser. You may leave it open; Stan can safely reuse the same authenticated session.",
        }

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    port = _free_debug_port()
    args = [
        str(browser["path"]),
        f"--user-data-dir={PROFILE_DIR}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",  # CDP port itself is bound to loopback only
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 8.0
        while time.time() < deadline and not _debug_endpoint_alive(port):
            if not interruptible_wait(0.15):
                return {"opened": False, "reason": "Stan stopped while browser was starting"}
        ready = _debug_endpoint_alive(port)
        payload = {
            "pid": int(proc.pid),
            "browser": browser.get("name", "Browser"),
            "path": browser.get("path", ""),
            "profile": str(PROFILE_DIR),
            "debug_port": port,
            "started_at": time.time(),
        }
        _write_session(payload)
        if not ready:
            return {
                "opened": True,
                "pid": int(proc.pid),
                "browser": browser.get("name", "Browser"),
                "url": url,
                "profile": str(PROFILE_DIR),
                "debug_port": port,
                "state": "AUTHENTICATING",
                "reason": "Browser opened, but the local control endpoint is not ready yet. Complete login/2FA and retry the action cycle.",
            }
        return {
            "opened": True,
            "pid": int(proc.pid),
            "browser": browser.get("name", "Browser"),
            "url": url,
            "profile": str(PROFILE_DIR),
            "debug_port": port,
            "state": "READY_OR_AUTHENTICATING",
            "instruction": "Log in / complete 2FA normally. You may leave this Stan browser window open; automation reuses this same session.",
        }
    except Exception as exc:
        return {"opened": False, "reason": f"{type(exc).__name__}: {exc}"}


def browser_available() -> dict[str, Any]:
    browser = _find_browser()
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        playwright_ok = True
        playwright_reason = ""
    except Exception as exc:
        playwright_ok = False
        playwright_reason = f"Playwright unavailable: {type(exc).__name__}: {exc}"

    session = _read_session()
    port = int(session.get("debug_port", 0) or 0)
    cdp_alive = _debug_endpoint_alive(port)

    if os.name == "nt" and not browser:
        return {
            "available": False,
            "browser_found": False,
            "playwright_ok": playwright_ok,
            "state": "UNAVAILABLE",
            "reason": "No supported Chromium browser was found through Windows default-browser registry, App Paths, standard install folders or PATH.",
        }
    if not playwright_ok:
        return {
            "available": False,
            "browser_found": bool(browser),
            "browser": (browser or {}).get("name", ""),
            "path": (browser or {}).get("path", ""),
            "playwright_ok": False,
            "state": "UNAVAILABLE",
            "reason": playwright_reason,
        }
    return {
        "available": True,
        "browser_found": bool(browser) or os.name != "nt",
        "playwright_ok": True,
        "browser": (browser or {}).get("name", "Playwright Chromium"),
        "path": (browser or {}).get("path", ""),
        "source": (browser or {}).get("source", "playwright"),
        "state": "SESSION_CONNECTED" if cdp_alive else "AVAILABLE",
        "session_connected": cdp_alive,
        "debug_port": port if cdp_alive else 0,
    }


def _target_closed_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    hints = (
        "targetclosederror", "target page, context or browser has been closed", "target.createTarget".lower(),
        "browser has been closed", "context has been closed", "page has been closed", "browser disconnected",
    )
    return "targetclosed" in name or any(h in text for h in hints)


class BybitBrowserOperator:
    """Restricted, recoverable operator for the user's dedicated Bybit browser profile."""

    def __init__(self, *, background_only: bool = True) -> None:
        self._pw = None
        self._background_only = bool(background_only)
        self._context = None
        self._browser = None
        self._browser_info: dict[str, str] | None = None
        self._owns_context = False
        self._connection_mode = ""
        self._reconnects = 0
        self._last_error = ""

    def __enter__(self) -> "BybitBrowserOperator":
        from playwright.sync_api import sync_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser_info = _find_browser()
        self._connect_or_launch()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            # A CDP-connected authorization browser belongs to the user's visible Stan session;
            # disconnect from it without closing Chrome or logging the user out.
            if self._owns_context and self._context:
                try:
                    self._context.close()
                except Exception:
                    pass
        finally:
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._context = None
            self._browser = None
            self._pw = None

    def _connect_or_launch(self) -> None:
        if self._pw is None:
            raise RuntimeError("Playwright is not started")
        session = _read_session()
        port = int(session.get("debug_port", 0) or 0)
        if _debug_endpoint_alive(port):
            try:
                self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                contexts = list(self._browser.contexts)
                if not contexts:
                    raise RuntimeError("CDP browser has no reusable context")
                self._context = contexts[0]
                self._owns_context = False
                self._connection_mode = "cdp"
                return
            except Exception as exc:
                self._last_error = f"CDP connect failed: {type(exc).__name__}: {exc}"

        kwargs: dict[str, Any] = {
            "user_data_dir": str(PROFILE_DIR),
            # v4.6.7 automated reward checks must never pop a visible browser over the user's
            # game/work. If a visible authorized CDP session exists we reuse it; otherwise the
            # same dedicated profile is opened headlessly. Visible launch is explicit auth only.
            "headless": bool(self._background_only),
            "viewport": {"width": 1360, "height": 900},
        }
        if self._browser_info and self._browser_info.get("path"):
            kwargs["executable_path"] = self._browser_info["path"]
        try:
            self._context = self._pw.chromium.launch_persistent_context(**kwargs)
            self._owns_context = True
            self._connection_mode = "persistent"
        except Exception as exc:
            raise RuntimeError(
                "Stan could not acquire the dedicated Bybit browser profile. If an old pre-v4.5.5 Stan Bybit window is still open, close only that old Stan browser window once and press Authorize Bybit Browser again. "
                f"Original error: {type(exc).__name__}: {exc}"
            ) from exc

    def _reconnect(self) -> bool:
        self._reconnects += 1
        self._last_error = ""
        try:
            self._context = None
            self._browser = None
            self._connect_or_launch()
            page = self._raw_page(create=True)
            page.evaluate("1+1")
            return True
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _raw_page(self, *, create: bool = True):
        if self._context is None:
            raise RuntimeError("Browser operator is not started")
        pages = []
        try:
            pages = [p for p in list(self._context.pages) if not p.is_closed()]
        except Exception as exc:
            raise RuntimeError(f"Browser context is stale: {type(exc).__name__}: {exc}") from exc
        for page in pages:
            try:
                if _official_url(page.url):
                    return page
            except Exception:
                continue
        if pages:
            return pages[0]
        if not create:
            raise RuntimeError("No live browser page is available")
        return self._context.new_page()

    def _page(self):
        last: BaseException | None = None
        for attempt in range(2):
            try:
                page = self._raw_page(create=True)
                page.evaluate("1+1")
                return page
            except Exception as exc:
                last = exc
                if attempt == 0 and _target_closed_error(exc) and self._reconnect():
                    continue
                raise
        raise RuntimeError(f"Could not acquire a live Bybit browser page: {last}")

    def health(self) -> dict[str, Any]:
        try:
            page = self._page()
            return {
                "state": "READY",
                "context_alive": True,
                "page_alive": True,
                "page_count": len(list(self._context.pages)) if self._context else 0,
                "active_host": (urlparse(page.url).hostname or "") if getattr(page, "url", "") else "",
                "connection_mode": self._connection_mode,
                "reconnects": self._reconnects,
                "last_error": self._last_error,
                "background_only": self._background_only,
            }
        except Exception as exc:
            return {
                "state": "STALE",
                "context_alive": False,
                "page_alive": False,
                "page_count": 0,
                "active_host": "",
                "connection_mode": self._connection_mode,
                "reconnects": self._reconnects,
                "last_error": f"{type(exc).__name__}: {exc}",
                "background_only": self._background_only,
            }

    def inspect(self, url: str) -> dict[str, Any]:
        if not _official_url(url):
            raise ValueError("Browser Operator is restricted to official Bybit domains.")
        last: BaseException | None = None
        for attempt in range(2):
            try:
                page = self._page()
                # Never call bring_to_front() in automated reward cycles. Navigation happens
                # in the dedicated background page so Stan cannot steal OS/browser focus.
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_timeout(1800)
                except Exception:
                    pass
                final_url = page.url
                if not _official_url(final_url):
                    raise RuntimeError("Bybit page redirected outside the official allowlist.")
                title = page.title()[:300]
                text = page.locator("body").inner_text(timeout=10000)[:24000]
                low = text.lower()
                login_required = any(x in low for x in ("log in", "login", "sign in")) and not any(x in low for x in ("log out", "logout"))
                human_verification = any(x in low for x in ("captcha", "verify you are human", "security verification", "two-factor", "2fa", "verification code"))
                candidates: list[dict[str, str]] = []
                for selector in ("button", "a"):
                    loc = page.locator(selector)
                    count = min(loc.count(), 250)
                    for i in range(count):
                        try:
                            el = loc.nth(i)
                            label = re.sub(r"\s+", " ", (el.inner_text(timeout=1000) or "").strip())[:180]
                            if not label:
                                continue
                            ll = label.lower()
                            if any(word in ll for word in SAFE_ACTION_WORDS) and not any(word in ll for word in DANGEROUS_ACTION_WORDS):
                                try:
                                    context = el.evaluate(r"""(e) => {
                                      let n=e;
                                      for (let i=0;i<5 && n;i++,n=n.parentElement) {
                                        const t=((n.innerText||'').replace(/\s+/g,' ').trim());
                                        if (t.length >= 40) return t.slice(0,1400);
                                      }
                                      return '';
                                    }""")
                                except Exception:
                                    context = ""
                                candidates.append({"text": label, "selector": selector, "index": i, "context": re.sub(r"\s+", " ", str(context or "")).strip()[:1400]})
                        except Exception:
                            continue
                seen: set[str] = set()
                unique: list[dict[str, str]] = []
                for c in candidates:
                    key = c["text"].casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    unique.append(c)
                return {
                    "url": final_url,
                    "title": title,
                    "text": text,
                    "browser": (self._browser_info or {}).get("name", "Playwright Chromium"),
                    "login_required": login_required,
                    "human_verification": human_verification,
                    "safe_action_candidates": unique[:30],
                    "browser_health": self.health(),
                }
            except Exception as exc:
                last = exc
                if attempt == 0 and _target_closed_error(exc) and self._reconnect():
                    continue
                raise
        raise RuntimeError(f"Browser inspect failed after recovery: {last}")

    def inspect_rewards_hub(self) -> dict[str, Any]:
        return self.inspect("https://www.bybit.com/en/rewards_hub")

    @staticmethod
    def infer_action_state(before: dict[str, Any], after: dict[str, Any], button_text: str) -> dict[str, Any]:
        label = re.sub(r"\s+", " ", str(button_text or "")).strip()
        low_label = label.lower()
        before_labels = {str(x.get("text", "")).casefold() for x in list(before.get("safe_action_candidates") or []) if isinstance(x, dict)}
        after_labels = {str(x.get("text", "")).casefold() for x in list(after.get("safe_action_candidates") or []) if isinstance(x, dict)}
        low = str(after.get("text", ""))[:24000].lower()
        disappeared = label.casefold() in before_labels and label.casefold() not in after_labels
        claimed_words = ("claimed", "claim successful", "successfully claimed", "reward received", "credited")
        registered_words = ("registered", "registration successful", "successfully registered", "joined", "participating", "enrolled")
        completed_words = ("completed", "check-in successful", "checked in", "task complete", "done")
        if "claim" in low_label and (disappeared or any(x in low for x in claimed_words)):
            return {"state": "CLAIMED", "verified": True, "evidence": "claim control changed/disappeared or page confirms reward claim"}
        if any(x in low_label for x in ("register", "join", "participate", "enroll")) and (disappeared or any(x in low for x in registered_words)):
            return {"state": "REGISTERED", "verified": True, "evidence": "registration control changed/disappeared or page confirms participation"}
        if any(x in low_label for x in ("check in", "check-in")) and (disappeared or any(x in low for x in completed_words + claimed_words)):
            return {"state": "COMPLETED", "verified": True, "evidence": "check-in control changed/disappeared or page confirms completion"}
        if "spin" in low_label:
            if any(x in low for x in claimed_words):
                return {"state": "CLAIMED", "verified": True, "evidence": "page confirms reward after spin"}
            if disappeared:
                return {"state": "COMPLETED", "verified": True, "evidence": "spin chance was consumed and control changed"}
        return {"state": "ACTION_SENT_UNVERIFIED", "verified": False, "evidence": "click sent; post-action page did not provide strong completion evidence"}

    def wait_for_authentication(self, timeout_seconds: int = 120) -> dict[str, Any]:
        deadline = time.time() + max(5, int(timeout_seconds))
        while time.time() < deadline:
            if runtime_stop_requested():
                return {"ready": False, "stopped": True, "reason": "manual STOP active"}
            try:
                page = self._page()
                text = page.locator("body").inner_text(timeout=5000)[:16000].lower()
                login_required = any(x in text for x in ("log in", "login", "sign in")) and not any(x in text for x in ("log out", "logout"))
                human_verification = any(x in text for x in ("captcha", "verify you are human", "security verification", "two-factor", "2fa", "verification code"))
                if not login_required and not human_verification:
                    return {"ready": True, "url": page.url}
            except Exception as exc:
                if _target_closed_error(exc) and self._reconnect():
                    continue
            if not interruptible_wait(0.5):
                return {"ready": False, "stopped": True, "reason": "Stan stopped during browser authentication"}
        try:
            url = self._page().url
        except Exception:
            url = ""
        return {"ready": False, "reason": "Bybit login/2FA was not completed within the visible authentication window", "url": url}

    def click_safe_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        label = re.sub(r"\s+", " ", str(candidate.get("text") or "").strip())
        low = label.lower()
        selector = str(candidate.get("selector") or "button")
        try:
            index = int(candidate.get("index", -1))
        except Exception:
            index = -1
        if selector not in {"button", "a"}:
            raise ValueError("Unsupported inspected selector")
        if not label or not any(word in low for word in SAFE_ACTION_WORDS):
            raise ValueError("Requested browser action is not on the safe reward-action allowlist.")
        if any(word in low for word in DANGEROUS_ACTION_WORDS):
            raise ValueError("Requested browser action may move funds or enroll in a financial product and is blocked.")
        for attempt in range(2):
            try:
                page = self._page()
                loc = page.locator(selector)
                if index < 0 or index >= loc.count():
                    return self.click_safe_text(label)
                el = loc.nth(index)
                actual = re.sub(r"\s+", " ", (el.inner_text(timeout=1000) or "").strip())
                if label.casefold() not in actual.casefold() and actual.casefold() not in label.casefold():
                    return {"clicked": False, "text": label, "url": page.url, "reason": "inspected control changed before click"}
                el.click(timeout=8000)
                page.wait_for_timeout(1500)
                return {"clicked": True, "text": actual, "url": page.url}
            except Exception as exc:
                if attempt == 0 and _target_closed_error(exc) and self._reconnect():
                    continue
                try:
                    url = self._page().url
                except Exception:
                    url = ""
                return {"clicked": False, "text": label, "url": url, "reason": f"exact safe control not clickable: {type(exc).__name__}: {exc}"}
        return {"clicked": False, "text": label, "url": "", "reason": "safe action failed after browser recovery"}

    def click_safe_text(self, text: str) -> dict[str, Any]:
        label = re.sub(r"\s+", " ", (text or "").strip())
        low = label.lower()
        if not label or not any(word in low for word in SAFE_ACTION_WORDS):
            raise ValueError("Requested browser action is not on the safe reward-action allowlist.")
        if any(word in low for word in DANGEROUS_ACTION_WORDS):
            raise ValueError("Requested browser action may move funds or enroll in a financial product and is blocked.")
        for attempt in range(2):
            try:
                page = self._page()
                for selector in ("button", "a"):
                    loc = page.locator(selector).filter(has_text=label)
                    for i in range(min(loc.count(), 20)):
                        try:
                            el = loc.nth(i)
                            actual = re.sub(r"\s+", " ", (el.inner_text(timeout=1000) or "").strip())
                            if actual.casefold() != label.casefold() and label.casefold() not in actual.casefold():
                                continue
                            el.click(timeout=8000)
                            page.wait_for_timeout(1500)
                            return {"clicked": True, "text": actual, "url": page.url}
                        except Exception as exc:
                            if _target_closed_error(exc):
                                raise
                            continue
                return {"clicked": False, "text": label, "url": page.url, "reason": "safe action control not found/clickable"}
            except Exception as exc:
                if attempt == 0 and _target_closed_error(exc) and self._reconnect():
                    continue
                return {"clicked": False, "text": label, "url": "", "reason": f"browser action failed: {type(exc).__name__}: {exc}"}
        return {"clicked": False, "text": label, "url": "", "reason": "safe action failed after browser recovery"}
