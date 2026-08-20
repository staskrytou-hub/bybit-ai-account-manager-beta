from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v460-integrity-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def _bybit_rows(now_ms: int, interval_ms: int = 900_000, n_closed: int = 100) -> list[list[str]]:
    # Oldest -> newest first, then reverse to Bybit newest-first response order.
    forming_start = (now_ms // interval_ms) * interval_ms
    oldest = forming_start - n_closed * interval_ms
    rows: list[list[str]] = []
    price = 100.0
    for i in range(n_closed):
        start = oldest + i * interval_ms
        close = price + i * 0.05
        volume = 100.0 + i
        rows.append([str(start), str(close - 0.02), str(close + 0.08), str(close - 0.08), str(close), str(volume), str(volume * close)])
    # The forming candle is intentionally absurd and almost empty. If it leaks into
    # structural evidence, volume ratio/returns/price will be visibly wrong.
    rows.append([str(forming_start), "105", "150", "50", "140", "0.01", "1.4"])
    return list(reversed(rows))


def main() -> None:
    import market_analysis as ma
    from market_analysis import parse_klines, completed_klines
    from risk_engine import evaluate_trade_candidate
    from trading_config import save_trading_settings
    from research_store import set_research_state
    import promotion_lifecycle as pl

    now_ms = int(time.time() * 1000)
    rows = _bybit_rows(now_ms)
    parsed = parse_klines(rows)
    completed = completed_klines(parsed, "15", now_ms=now_ms)
    assert len(parsed) == 101
    assert len(completed) == 100
    assert float(completed[-1]["close"]) < 110.0
    assert int(completed[-1]["start"]) != int(parsed[-1]["start"])

    class FakeMarketClient:
        def __init__(self, *args, **kwargs):
            pass
        def get_kline(self, *args, **kwargs):
            return rows
        def get_ticker(self, *args, **kwargs):
            return {"lastPrice": "140", "markPrice": "140", "indexPrice": "140", "bid1Price": "139.9", "ask1Price": "140.0", "price24hPcnt": "0.01", "turnover24h": "1000000", "fundingRate": "0.0001"}
        def get_orderbook(self, *args, **kwargs):
            return {"b": [["139.9", "10"]], "a": [["140.0", "10"]]}
        def get_open_interest(self, *args, **kwargs):
            return [{"openInterest": "100"}, {"openInterest": "101"}]
        def get_long_short_ratio(self, *args, **kwargs):
            return [{"buyRatio": "0.5", "sellRatio": "0.5"}]
        def get_funding_history(self, *args, **kwargs):
            return [{"fundingRate": "0.0001"}]

    original_client = ma.BybitClient
    try:
        ma.BybitClient = FakeMarketClient
        futures = ma.build_market_snapshot("TESTUSDT", "15")
    finally:
        ma.BybitClient = original_client
    assert futures["forming_candle_excluded"] is True, futures
    assert futures["price"] == 140.0, futures  # live price remains live
    assert futures["signal_price"] < 110.0, futures  # structural evidence is closed-candle only
    assert futures["closed_candle_start_ms"] != futures["forming_candle_start_ms"], futures
    assert futures["evidence_candle_policy"] == "completed_candles_only_v460", futures
    assert futures["volume_ratio_20"] > 0.5, futures

    futures_ai_text = (ROOT / "trading_ai.py").read_text(encoding="utf-8-sig")
    spot_ai_text = (ROOT / "spot_ai.py").read_text(encoding="utf-8-sig")
    assert "signal_price" in futures_ai_text and "evidence_candle_policy" in futures_ai_text
    assert "signal_price" in spot_ai_text and "evidence_candle_policy" in spot_ai_text

    spot_text = (ROOT / "spot_engine.py").read_text(encoding="utf-8")
    assert "completed_klines(all_candles, interval)" in spot_text
    assert '"evidence_candle_policy": "completed_candles_only_v460"' in spot_text

    save_trading_settings({"mode": "autopilot_live"})
    set_research_state("bootstrap_complete", "1")
    assessment = {
        "action": "long", "confidence": 0.9, "entry": 10.0, "stop_loss": 9.8, "take_profit": 10.5,
        "analysis_symbol": "CYSUSDT", "analysis_interval": "15", "analysis_closed_candle_start_ms": 111,
    }
    snapshot = {"symbol": "SOLUSDT", "interval": "15", "closed_candle_start_ms": 222, "price": 10.0, "spread_bps": 1.0, "captured_at_ms": 0}
    instrument = {"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"}, "leverageFilter": {"maxLeverage": "10", "leverageStep": "0.01"}}
    risk = evaluate_trade_candidate(assessment, snapshot, instrument, equity=100.0, adaptive_risk_pct=1.5, leverage_cap=2.0, exposure_cap_pct=125.0)
    assert risk["allowed"] is False, risk
    assert any("state coherence mismatch" in x for x in risk["reasons"]), risk

    store: dict[str, object] = {}
    original_get, original_set = pl.get_state, pl.set_state
    try:
        pl.get_state = lambda key, default=None: store.get(key, default)
        pl.set_state = lambda key, value: store.__setitem__(key, value)
        campaign = {"campaign_key": "pending-join", "name": "Pending Join"}
        pl.update_lifecycle(campaign, "ACTION_SENT_UNVERIFIED", evidence="clicked", action="Join Now", url="https://www.bybit.com/en/rewards_hub")
        after = pl.update_lifecycle(campaign, "DISCOVERED", evidence="rediscovered", action="", url="https://www.bybit.com/en/rewards_hub")
        assert after["state"] == "ACTION_SENT_UNVERIFIED", after
        assert after["last_action"] == "Join Now", after
    finally:
        pl.get_state, pl.set_state = original_get, original_set

    cfg_text = (ROOT / "trading_config.py").read_text(encoding="utf-8")
    executor_text = (ROOT / "promotion_executor.py").read_text(encoding="utf-8")
    account_text = (ROOT / "account_os.py").read_text(encoding="utf-8")
    assert '"browser_cycle_timeout_seconds": 420' in cfg_text
    assert "deadline_monotonic" in executor_text
    assert "_verify_pending_without_click" in executor_text
    assert "watchdog_deadline_at" in account_text

    print("v4.6.0 closed-candle integrity / state coherence / browser watchdog smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
