from __future__ import annotations

import math
import statistics
import time
from typing import Any

from bybit_client import BybitClient


def _f(value: Any, default: float = 0.0) -> float:
    try: return float(value)
    except Exception: return default


def ema(values: list[float], period: int) -> float:
    if not values: return 0.0
    alpha = 2.0 / (period + 1.0)
    out = values[0]
    for v in values[1:]: out = alpha * v + (1.0 - alpha) * out
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1: return 50.0
    diffs = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in diffs[-period:]]
    losses = [max(-x, 0.0) for x in diffs[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0: return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[dict[str, float]], period: int = 14) -> float:
    if len(candles) < 2: return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i-1]
        trs.append(max(cur['high'] - cur['low'], abs(cur['high'] - prev['close']), abs(cur['low'] - prev['close'])))
    sample = trs[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def _interval_to_period(interval: str) -> str:
    return {"1":"5min","3":"5min","5":"5min","15":"15min","30":"30min","60":"1h","120":"4h","240":"4h","D":"1d"}.get(interval, "15min")


def parse_klines(rows: list[list[str]]) -> list[dict[str, float]]:
    candles: list[dict[str, float]] = []
    for row in reversed(rows):
        if len(row) < 7: continue
        candles.append({
            "start": _f(row[0]), "open": _f(row[1]), "high": _f(row[2]), "low": _f(row[3]),
            "close": _f(row[4]), "volume": _f(row[5]), "turnover": _f(row[6]),
        })
    return candles


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def _slope_pct(values: list[float]) -> float:
    if len(values) < 3 or not values[-1]:
        return 0.0
    n = len(values)
    sx = n * (n - 1) / 2.0
    sy = sum(values)
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sxy = sum(i * v for i, v in enumerate(values))
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / den if den else 0.0
    return slope / max(abs(values[-1]), 1e-12) * 100.0


def _return_pct(closes: list[float], bars: int) -> float:
    if len(closes) <= bars or closes[-1-bars] == 0:
        return 0.0
    return (closes[-1] / closes[-1-bars] - 1.0) * 100.0


def _interval_ms(interval: str) -> int:
    raw = str(interval or "15").upper()
    if raw.isdigit():
        return max(1, int(raw)) * 60_000
    return {"D": 86_400_000, "W": 7 * 86_400_000}.get(raw, 15 * 60_000)


def completed_klines(candles: list[dict[str, float]], interval: str, *, now_ms: int | None = None) -> list[dict[str, float]]:
    """Return only fully closed candles from a Bybit kline series."""
    rows = list(candles or [])
    if not rows:
        return rows
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    span = _interval_ms(interval)
    last_start = int(float(rows[-1].get("start", 0) or 0))
    if last_start > 0 and last_start + span > now - 2_000:
        return rows[:-1]
    return rows


def build_market_snapshot(symbol: str, interval: str = "15", *, testnet_market_data: bool = False) -> dict[str, Any]:
    client = BybitClient(testnet=testnet_market_data)
    rows = client.get_kline(symbol, interval=interval, limit=240)
    all_candles = parse_klines(rows)
    candles = completed_klines(all_candles, interval)
    if len(candles) < 80:
        raise RuntimeError(f"Not enough completed Bybit kline history for {symbol}: {len(candles)} candles")
    ticker = client.get_ticker(symbol)
    orderbook = client.get_orderbook(symbol, limit=25)
    period = _interval_to_period(interval)
    oi = client.get_open_interest(symbol, interval_time=period, limit=10)
    ratios = client.get_long_short_ratio(symbol, period=period, limit=10)
    funding = client.get_funding_history(symbol, limit=3)

    closes = [c['close'] for c in candles]
    volumes = [c['volume'] for c in candles]
    signal_last = closes[-1]
    live_price = _f(ticker.get('lastPrice'), signal_last)
    # Legacy indicators remain observable measurements only. They are NOT strategy gates in v4.4.
    e20 = ema(closes[-80:], 20)
    e50 = ema(closes[-120:], 50)
    r14 = rsi(closes, 14)
    a14 = atr(candles, 14)
    atr_pct = (a14 / signal_last * 100.0) if signal_last else 0.0

    ret1 = _return_pct(closes, 1)
    ret4 = _return_pct(closes, 4)
    ret12 = _return_pct(closes, 12)
    ret48 = _return_pct(closes, 48)
    bar_rets = [((closes[i] / closes[i-1]) - 1.0) * 100.0 if closes[i-1] else 0.0 for i in range(1, len(closes))]
    realized_vol_20 = _std(bar_rets[-20:])
    slope20 = _slope_pct(closes[-20:])
    slope50 = _slope_pct(closes[-50:])

    recent_vol = volumes[-20:]
    base_vol = recent_vol[:-1] if len(recent_vol) > 1 else recent_vol
    vol_mean = _mean(base_vol) or 1.0
    vol_std = _std(base_vol)
    volume_ratio = volumes[-1] / vol_mean if volumes else 1.0
    volume_z = (volumes[-1] - vol_mean) / vol_std if vol_std > 1e-12 else 0.0

    prior20 = candles[-21:-1]
    prior50 = candles[-51:-1]
    hi20 = max((x['high'] for x in prior20), default=signal_last)
    lo20 = min((x['low'] for x in prior20), default=signal_last)
    hi50 = max((x['high'] for x in prior50), default=signal_last)
    lo50 = min((x['low'] for x in prior50), default=signal_last)
    range_pos20 = (signal_last - lo20) / max(hi20 - lo20, 1e-12)
    range_pos50 = (signal_last - lo50) / max(hi50 - lo50, 1e-12)
    breakout20 = (signal_last - hi20) / max(a14, 1e-12)
    breakdown20 = (lo20 - signal_last) / max(a14, 1e-12)
    vwap_num = sum(c * v for c, v in zip(closes[-20:], volumes[-20:]))
    vwap_den = sum(volumes[-20:]) or 1.0
    vwap20 = vwap_num / vwap_den
    vwap_dist = ((signal_last / vwap20) - 1.0) * 100.0 if vwap20 else 0.0
    drawdown20 = ((signal_last / hi20) - 1.0) * 100.0 if hi20 else 0.0
    rebound20 = ((signal_last / lo20) - 1.0) * 100.0 if lo20 else 0.0
    cur = candles[-1]
    body_strength = abs(cur['close'] - cur['open']) / max(cur['high'] - cur['low'], 1e-12)

    bids = orderbook.get('b') or []
    asks = orderbook.get('a') or []
    bid = _f(bids[0][0]) if bids else _f(ticker.get('bid1Price'))
    ask = _f(asks[0][0]) if asks else _f(ticker.get('ask1Price'))
    mid = (bid + ask) / 2.0 if bid and ask else live_price
    spread_bps = ((ask - bid) / mid * 10000.0) if bid and ask and mid else 0.0
    bid_depth = sum(_f(x[1]) for x in bids[:10])
    ask_depth = sum(_f(x[1]) for x in asks[:10])
    orderbook_imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-12)

    oi_vals = [_f(x.get('openInterest')) for x in reversed(oi)]
    oi_change_pct = ((oi_vals[-1] / oi_vals[0]) - 1.0) * 100.0 if len(oi_vals) >= 2 and oi_vals[0] else 0.0
    latest_ratio = ratios[0] if ratios else {}
    long_ratio = _f(latest_ratio.get('buyRatio'), 0.5)
    short_ratio = _f(latest_ratio.get('sellRatio'), 0.5)
    latest_funding = _f((funding[0] if funding else {}).get('fundingRate'), _f(ticker.get('fundingRate')))

    if ret4 > 0 and oi_change_pct > 0: oi_price_regime = "price_up_oi_up"
    elif ret4 < 0 and oi_change_pct > 0: oi_price_regime = "price_down_oi_up"
    elif ret4 > 0 and oi_change_pct < 0: oi_price_regime = "price_up_oi_down"
    elif ret4 < 0 and oi_change_pct < 0: oi_price_regime = "price_down_oi_down"
    else: oi_price_regime = "flat"

    # v4.4: structural/evidence strength, not an EMA/RSI strategy score.
    trend_evidence = min(1.0, (abs(slope20) * 18.0 + abs(slope50) * 12.0) / 2.0)
    structure_evidence = min(1.0, max(abs(breakout20), abs(breakdown20), abs(range_pos20 - 0.5) * 1.6))
    participation_evidence = min(1.0, max(0.0, volume_z) / 3.0 + min(abs(oi_change_pct) / 8.0, 0.5))
    microstructure_evidence = min(1.0, abs(orderbook_imbalance) * 2.0)
    setup_strength = max(0.0, min(1.0, 0.30 * trend_evidence + 0.30 * structure_evidence + 0.25 * participation_evidence + 0.15 * microstructure_evidence))
    directional_raw = slope20 * 8.0 + ret4 * 0.08 + orderbook_imbalance * 0.35 + math.copysign(min(abs(oi_change_pct) / 10.0, 0.25), ret4 or slope20 or 1.0)
    directional_score = max(-1.0, min(1.0, directional_raw))

    closed_candle_start = int(candles[-1]['start'])
    forming_excluded = len(all_candles) > len(candles)
    forming_start = int(all_candles[-1]['start']) if forming_excluded and all_candles else 0
    return {
        "symbol": symbol.upper(), "interval": interval, "captured_at_ms": int(time.time()*1000),
        "closed_candle_start_ms": closed_candle_start,
        "forming_candle_start_ms": forming_start, "forming_candle_excluded": forming_excluded,
        "price": live_price, "signal_price": signal_last, "mark_price": _f(ticker.get('markPrice'), live_price), "index_price": _f(ticker.get('indexPrice'), live_price),
        "bid": bid, "ask": ask, "spread_bps": round(spread_bps, 4),
        "orderbook_imbalance_10": round(orderbook_imbalance, 5),
        "ema20": round(e20, 8), "ema50": round(e50, 8), "rsi14": round(r14, 3),
        "atr14": round(a14, 8), "atr_pct": round(atr_pct, 4),
        "return_1_candle_pct": round(ret1, 4), "return_4_candle_pct": round(ret4, 4),
        "return_12_candle_pct": round(ret12, 4), "return_48_candle_pct": round(ret48, 4),
        "return_1_pct": round(ret1, 4), "return_4_pct": round(ret4, 4), "return_12_pct": round(ret12, 4), "return_48_pct": round(ret48, 4),
        "trend_slope_20_pct": round(slope20, 6), "trend_slope_50_pct": round(slope50, 6),
        "realized_vol_20_pct": round(realized_vol_20, 5),
        "volume_ratio_20": round(volume_ratio, 4), "volume_z_20": round(volume_z, 4),
        "range_position_20": round(range_pos20, 4), "range_position_50": round(range_pos50, 4),
        "breakout_20_atr": round(breakout20, 4), "breakdown_20_atr": round(breakdown20, 4),
        "vwap_distance_20_pct": round(vwap_dist, 4), "drawdown_from_20_high_pct": round(drawdown20, 4),
        "rebound_from_20_low_pct": round(rebound20, 4), "body_strength": round(body_strength, 4),
        "open_interest_change_pct": round(oi_change_pct, 4), "oi_price_regime": oi_price_regime,
        "long_ratio": round(long_ratio, 4), "short_ratio": round(short_ratio, 4), "funding_rate": latest_funding,
        "directional_score": round(directional_score, 4), "setup_strength": round(setup_strength, 4),
        "local_bias": "long" if directional_score > 0.15 else ("short" if directional_score < -0.15 else "neutral"),
        "ticker_24h_pct": _f(ticker.get('price24hPcnt')) * 100.0,
        "turnover_24h": _f(ticker.get('turnover24h')),
        "candles_count": len(candles),
        "evidence_candle_policy": "completed_candles_only_v460",
        "evidence_model": "v4.6_structural_closed_candle_integrity",
    }
