from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from universe_scanner import choose_rotation_candidate
import browser_operator


def main() -> None:
    rows = [
        {"symbol": "AAAUSDT", "scanner_score": 0.80},
        {"symbol": "BBBUSDT", "scanner_score": 0.77},
        {"symbol": "CCCUSDT", "scanner_score": 0.75},
    ]
    picked, meta = choose_rotation_candidate(rows, recent_symbols=["AAAUSDT"], rotation_margin=0.08, dominance_margin=0.12)
    assert picked["symbol"] == "BBBUSDT", (picked, meta)
    assert meta["mode"] == "rotation", meta

    dominant = [
        {"symbol": "AAAUSDT", "scanner_score": 0.91},
        {"symbol": "BBBUSDT", "scanner_score": 0.70},
    ]
    picked2, meta2 = choose_rotation_candidate(dominant, recent_symbols=["AAAUSDT"])
    assert picked2["symbol"] == "AAAUSDT", (picked2, meta2)
    assert meta2["mode"] == "dominant", meta2

    class Proc:
        pid = 4321

    with patch.object(browser_operator, "_find_browser", return_value={"name": "Google Chrome", "path": "/fake/chrome.exe"}), \
         patch.object(browser_operator.subprocess, "Popen", return_value=Proc()) as popen:
        out = browser_operator.launch_authorization_browser()
        assert out["opened"] is True and out["pid"] == 4321, out
        args = popen.call_args.args[0]
        assert any(str(x).startswith("--user-data-dir=") for x in args), args
        assert "https://www.bybit.com/en/rewards_hub" in args, args

    root = Path(__file__).resolve().parents[1]
    proposal = (root / "trade_proposal.py").read_text(encoding="utf-8")
    sig_start = proposal.index('def _proposal_signature')
    sig_end = proposal.index('def _veto_key', sig_start)
    assert "closed_candle" not in proposal[sig_start:sig_end], "candle timestamp still contaminates proposal evidence signature"
    ui = (root / "account_os_ui.py").read_text(encoding="utf-8")
    assert "Authorize Bybit Browser" in ui
    server = (root / "core_server.py").read_text(encoding="utf-8")
    assert 'action=="authorize_bybit_browser"' in server
    print("v4.5.3 rotation + evidence-dedupe + auth-handoff smoke: PASS")


if __name__ == "__main__":
    main()
