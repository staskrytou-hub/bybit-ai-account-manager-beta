from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v441-browser-eval-"))
sys.path.insert(0, str(ROOT))


def main() -> None:
    import browser_operator as bo

    assert bo._official_url("https://www.bybit.com/en/promo")
    assert bo._official_url("https://announcements.bybit.com/en/article/example")
    assert bo._official_url("https://www.bybit.eu/en-EU/promo")
    assert not bo._official_url("https://evil.example/bybit")

    local = TEMP / "LocalAppData"
    chrome = local / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.write_bytes(b"fake")

    old_local = os.environ.get("LOCALAPPDATA")
    old_pf = os.environ.get("PROGRAMFILES")
    old_pfx86 = os.environ.get("PROGRAMFILES(X86)")
    try:
        os.environ["LOCALAPPDATA"] = str(local)
        os.environ["PROGRAMFILES"] = str(TEMP / "ProgramFiles")
        os.environ["PROGRAMFILES(X86)"] = str(TEMP / "ProgramFilesX86")
        found = bo._find_browser(force_windows=True)
        assert found, found
        assert found["name"] == "Google Chrome", found
        assert Path(found["path"]) == chrome, found
    finally:
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
        if old_pf is None:
            os.environ.pop("PROGRAMFILES", None)
        else:
            os.environ["PROGRAMFILES"] = old_pf
        if old_pfx86 is None:
            os.environ.pop("PROGRAMFILES(X86)", None)
        else:
            os.environ["PROGRAMFILES(X86)"] = old_pfx86

    print("v4.4.1 browser discovery / Bybit allowlist smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
