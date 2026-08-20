from __future__ import annotations

import math
from typing import Any

from bybit_client import BybitClient
from market_analysis import atr, parse_klines, completed_klines


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def scan_linear_universe(*, top_n: int = 12, testnet: bool = False) -> list[dict[str, Any]]:
    client = BybitClient(testnet=testnet)
    tickers = client.get_tickers("linear")
    candidates: list[dict[str, Any]] = []
    for t in tickers:
        symbol = str(t.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        turnover = _f(t.get("turnover24h"))
        last = _f(t.get("lastPrice"))
        bid = _f(t.get("bid1Price"))
        ask = _f(t.get("ask1Price"))
        if turnover <= 0 or last <= 0:
            continue
        spread_bps = ((ask - bid) / ((ask + bid) / 2) * 10000) if ask > 0 and bid > 0 else 999.0
        candidates.append({
            "symbol": symbol,
            "last": last,
            "turnover_24h": turnover,
            "price_24h_pct": _f(t.get("price24hPcnt")) * 100.0,
            "funding_rate": _f(t.get("fundingRate")),
            "open_interest": _f(t.get("openInterest")),
            "spread_bps": spread_bps,
        })
    candidates.sort(key=lambda x: x["turnover_24h"], reverse=True)
    return candidates[: max(3, min(int(top_n), 50))]


def _slope_pct(values: list[float]) -> float:
    if len(values) < 3 or values[-1] == 0:
        return 0.0
    n = len(values)
    sx = n * (n - 1) / 2.0
    sy = sum(values)
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sxy = sum(i * v for i, v in enumerate(values))
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / den if den else 0.0
    return slope / max(abs(values[-1]), 1e-12) * 100.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))


def timeframe_regime(symbol: str, interval: str, *, testnet: bool = False, limit: int = 240) -> dict[str, Any]:
    """Structural timeframe view. No EMA/RSI is required for regime classification."""
    client = BybitClient(testnet=testnet)
    all_candles = parse_klines(client.get_kline(symbol, interval=interval, limit=limit))
    candles = completed_klines(all_candles, interval)
    if len(candles) < 60:
        return {"symbol": symbol, "interval": interval, "error": "insufficient_completed_history"}
    closes = [float(x["close"]) for x in candles]
    vols = [float(x["volume"]) for x in candles]
    last = closes[-1]
    a14 = atr(candles, 14)
    atr_pct = (a14 / last * 100.0) if last else 0.0
    ret4 = (last / closes[-5] - 1.0) * 100.0 if len(closes) > 5 and closes[-5] else 0.0
    ret20 = (last / closes[-21] - 1.0) * 100.0 if len(closes) > 21 and closes[-21] else 0.0
    slope20 = _slope_pct(closes[-20:])
    slope50 = _slope_pct(closes[-50:])
    rets = [((closes[i] / closes[i-1]) - 1.0) * 100.0 if closes[i-1] else 0.0 for i in range(1, len(closes))]
    vol20 = _std(rets[-20:])
    prior20 = candles[-21:-1]
    hi20 = max((float(x["high"]) for x in prior20), default=last)
    lo20 = min((float(x["low"]) for x in prior20), default=last)
    range_pos = (last - lo20) / max(hi20 - lo20, 1e-12)
    vol_base = vols[-21:-1] if len(vols) >= 21 else vols[:-1]
    vol_mean = sum(vol_base) / len(vol_base) if vol_base else 0.0
    volume_ratio = vols[-1] / vol_mean if vol_mean > 0 else 1.0

    directional = slope20 * 0.55 + slope50 * 0.25 + ret4 * 0.05 + ret20 * 0.01
    if directional > 0.015 and range_pos >= 0.55:
        trend = "bull"
    elif directional < -0.015 and range_pos <= 0.45:
        trend = "bear"
    else:
        trend = "mixed"
    return {
        "symbol": symbol, "interval": interval, "price": last,
        "trend_slope_20_pct": round(slope20, 6), "trend_slope_50_pct": round(slope50, 6),
        "return_4_bars_pct": round(ret4, 4), "return_20_bars_pct": round(ret20, 4),
        "realized_vol_20_pct": round(vol20, 5), "atr_pct": round(atr_pct, 4),
        "range_position_20": round(range_pos, 4), "volume_ratio_20": round(volume_ratio, 4),
        "closed_candle_start_ms": int(candles[-1].get("start", 0) or 0),
        "forming_candle_excluded": len(all_candles) > len(candles),
        "trend": trend, "regime_model": "structural_v460_closed_candle_integrity",
    }


def multi_timeframe_regime(symbol: str, intervals: list[str] | None = None, *, testnet: bool = False) -> dict[str, Any]:
    intervals = intervals or ["5", "15", "60", "240"]
    frames = [timeframe_regime(symbol, x, testnet=testnet) for x in intervals]
    valid = [x for x in frames if not x.get("error")]
    bulls = sum(1 for x in valid if x.get("trend") == "bull")
    bears = sum(1 for x in valid if x.get("trend") == "bear")
    alignment = (max(bulls, bears) / len(valid)) if valid else 0.0
    dominant = "bull" if bulls > bears else ("bear" if bears > bulls else "mixed")
    return {"symbol": symbol, "dominant": dominant, "alignment": round(alignment, 3), "frames": frames}



def choose_rotation_candidate(scored: list[dict[str, Any]], *, recent_symbols: list[str] | None = None, rotation_margin: float = 0.08, dominance_margin: float = 0.12) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose a live candidate without pinning Stan to one market forever.

    The best market still wins when it is clearly dominant. When several candidates are
    statistically close, Stan rotates through them so learning/research sees more than one
    asset instead of repeatedly spending AI calls on the same symbol. This never bypasses
    the Risk Engine and never promotes an infeasible candidate.
    """
    valid = [x for x in scored if float(x.get("scanner_score", 0) or 0) > 0 and not x.get("infeasible_for_safe_cap")]
    if not valid:
        fallback = scored[0] if scored else {"symbol": "BTCUSDT", "scanner_score": 0.0}
        return fallback, {"mode": "fallback", "reason": "no positive feasible scanner candidate"}
    top = valid[0]
    top_score = float(top.get("scanner_score", 0) or 0)
    second_score = float(valid[1].get("scanner_score", 0) or 0) if len(valid) > 1 else 0.0
    if len(valid) == 1 or top_score - second_score >= max(0.01, float(dominance_margin)):
        return top, {"mode": "dominant", "reason": "top candidate has a clear score advantage", "top_score": top_score, "second_score": second_score}
    close = [x for x in valid[:5] if top_score - float(x.get("scanner_score", 0) or 0) <= max(0.01, float(rotation_margin))]
    recent = [str(x).upper() for x in (recent_symbols or []) if str(x).strip()]
    avoid = set(recent[-2:])
    for candidate in close:
        if str(candidate.get("symbol", "")).upper() not in avoid:
            return candidate, {"mode": "rotation", "reason": "near-equal candidates rotated for broader live learning", "top_symbol": top.get("symbol"), "top_score": top_score, "selected_score": float(candidate.get("scanner_score", 0) or 0)}
    return top, {"mode": "top", "reason": "near-equal set exhausted; returning to highest score", "top_score": top_score}

def select_active_symbol(*, watchlist_size: int = 6, decision_interval: str = "15", testnet: bool = False, promotion_boosts: dict[str, float] | None = None, promotion_weight: float = 0.05, promotion_min_base_setup: float = 0.45, max_safe_notional_usdt: float | None = None, recent_symbols: list[str] | None = None, rotation_margin: float = 0.08, dominance_margin: float = 0.12) -> dict[str, Any]:
    universe = scan_linear_universe(top_n=max(5, watchlist_size), testnet=testnet)
    scored: list[dict[str, Any]] = []
    for rank, item in enumerate(universe[: max(3, watchlist_size)], start=1):
        symbol = str(item["symbol"])
        try:
            if max_safe_notional_usdt is not None and float(max_safe_notional_usdt) > 0:
                instrument = BybitClient(testnet=testnet).get_instrument(symbol)
                # Public exchange metadata is carried into the local proposal preflight so
                # v4.6.5 can block whole ineligible product families before paid AI.
                item["symbol_type"] = str(instrument.get("symbolType") or "")
                item["display_name"] = str(instrument.get("displayName") or "")
                item["contract_type"] = str(instrument.get("contractType") or "")
                lot = instrument.get("lotSizeFilter") or {}
                min_qty = _f(lot.get("minOrderQty"))
                min_notional_value = _f(lot.get("minNotionalValue"))
                min_notional = max(min_notional_value, min_qty * float(item.get("last", 0.0) or 0.0))
                item["min_order_notional_estimate"] = round(min_notional, 6)
                if min_notional > float(max_safe_notional_usdt) * 1.02:
                    scored.append({**item, "scanner_score": 0.0, "infeasible_for_safe_cap": True, "reason": "minimum order exceeds Stan safe notional cap"})
                    continue
            fast = timeframe_regime(symbol, decision_interval, testnet=testnet, limit=220)
            slow_interval = "60" if decision_interval in {"1", "3", "5", "15", "30"} else "240"
            slow = timeframe_regime(symbol, slow_interval, testnet=testnet, limit=220)
            fast_trend = str(fast.get("trend", "mixed")); slow_trend = str(slow.get("trend", "mixed"))
            alignment = 1.0 if fast_trend == slow_trend and fast_trend in {"bull", "bear"} else (0.35 if fast_trend in {"bull", "bear"} else 0.0)
            momentum = min(1.0, abs(float(fast.get("return_20_bars_pct", 0))) / 5.0)
            liquidity = max(0.0, 1.0 - (rank - 1) / max(1.0, float(watchlist_size)))
            spread_score = max(0.0, 1.0 - min(float(item.get("spread_bps", 999)) / 20.0, 1.0))
            base_score = 0.45 * alignment + 0.25 * momentum + 0.20 * liquidity + 0.10 * spread_score
            promo_hint = max(0.0, min(1.0, float((promotion_boosts or {}).get(symbol, 0.0))))
            # Promotions may only break ties among already-valid opportunities. They cannot turn a weak market into a candidate.
            promo_bonus = min(max(0.0, float(promotion_weight)), 0.10) * promo_hint if base_score >= float(promotion_min_base_setup) else 0.0
            score = min(1.0, base_score + promo_bonus)
            scored.append({**item, "fast": fast, "slow": slow, "base_scanner_score": round(base_score, 4), "promotion_hint": round(promo_hint, 4), "promotion_bonus": round(promo_bonus, 4), "scanner_score": round(score, 4)})
        except Exception as exc:
            scored.append({**item, "scanner_score": 0.0, "error": f"{type(exc).__name__}: {exc}"})
    scored.sort(key=lambda x: float(x.get("scanner_score", 0)), reverse=True)
    if scored:
        best, rotation = choose_rotation_candidate(scored, recent_symbols=recent_symbols, rotation_margin=rotation_margin, dominance_margin=dominance_margin)
    else:
        best = universe[0] if universe else {"symbol": "BTCUSDT"}
        rotation = {"mode": "fallback", "reason": "scanner returned no scored rows"}
    return {"selected_symbol": str(best.get("symbol", "BTCUSDT")), "candidates": scored, "rotation": rotation}
