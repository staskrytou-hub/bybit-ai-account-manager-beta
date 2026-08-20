from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from market_analysis import atr, ema, parse_klines, rsi


@dataclass(frozen=True)
class StrategySpec:
    key: str
    name: str
    description: str
    stop_atr: float
    target_atr: float
    max_hold_bars: int


STRATEGIES: list[StrategySpec] = [
    StrategySpec("trend_follow", "EMA Trend Continuation", "EMA20/50 aligned trend with RSI confirmation", 1.5, 2.4, 16),
    StrategySpec("breakout", "20-bar Volume Breakout", "20-bar range breakout with elevated volume", 1.4, 2.6, 14),
    StrategySpec("mean_revert", "ATR Mean Reversion", "RSI extreme plus ATR displacement from EMA20", 1.2, 1.8, 12),
    StrategySpec("momentum", "Volume Momentum", "EMA20 momentum with RSI and volume acceleration", 1.3, 2.1, 10),
]


def _sma(values: list[float], n: int) -> float:
    s = values[-n:]
    return sum(s) / len(s) if s else 0.0


def _features(candles: list[dict[str, float]], i: int) -> dict[str, float]:
    start = max(0, i - 120)
    sample = candles[start : i + 1]
    closes = [x["close"] for x in sample]
    vols = [x["volume"] for x in sample]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    r14 = rsi(closes, 14)
    a14 = atr(sample, 14)
    vol_avg = _sma(vols[:-1] if len(vols) > 1 else vols, 20) or 1.0
    vol_ratio = vols[-1] / vol_avg if vols else 1.0
    prior = candles[max(0, i - 20):i]
    prior_high = max((x["high"] for x in prior), default=sample[-1]["high"])
    prior_low = min((x["low"] for x in prior), default=sample[-1]["low"])
    ret4 = 0.0
    if len(closes) >= 5 and closes[-5]:
        ret4 = closes[-1] / closes[-5] - 1.0
    return {
        "close": closes[-1], "ema20": e20, "ema50": e50, "rsi": r14, "atr": a14,
        "vol_ratio": vol_ratio, "prior_high": prior_high, "prior_low": prior_low, "ret4": ret4,
    }


def _signal(key: str, f: dict[str, float]) -> str:
    c, e20, e50, r14, a14 = f["close"], f["ema20"], f["ema50"], f["rsi"], f["atr"]
    if a14 <= 0:
        return ""
    if key == "trend_follow":
        if c > e20 > e50 and 52 <= r14 <= 72:
            return "long"
        if c < e20 < e50 and 28 <= r14 <= 48:
            return "short"
    elif key == "breakout":
        if c > f["prior_high"] and f["vol_ratio"] >= 1.20:
            return "long"
        if c < f["prior_low"] and f["vol_ratio"] >= 1.20:
            return "short"
    elif key == "mean_revert":
        if r14 <= 30 and c < e20 - 1.1 * a14:
            return "long"
        if r14 >= 70 and c > e20 + 1.1 * a14:
            return "short"
    elif key == "momentum":
        if c > e20 and r14 >= 58 and f["vol_ratio"] >= 1.25 and f["ret4"] > 0:
            return "long"
        if c < e20 and r14 <= 42 and f["vol_ratio"] >= 1.25 and f["ret4"] < 0:
            return "short"
    return ""


def backtest_strategy(
    rows: list[list[str]],
    spec: StrategySpec,
    *,
    taker_fee_rate: float = 0.00055,
    slippage_bps: float = 1.0,
) -> dict[str, Any]:
    candles = parse_klines(rows)
    if len(candles) < 180:
        return {"strategy": spec.key, "name": spec.name, "trades": 0, "error": "insufficient_history"}

    outcomes: list[float] = []
    wins = losses = 0
    i = 120
    while i < len(candles) - 2:
        f = _features(candles, i)
        side = _signal(spec.key, f)
        if not side:
            i += 1
            continue
        next_bar = candles[i + 1]
        entry = float(next_bar["open"])
        risk_dist = float(f["atr"]) * spec.stop_atr
        if entry <= 0 or risk_dist <= 0:
            i += 1
            continue
        if side == "long":
            stop = entry - risk_dist
            target = entry + float(f["atr"]) * spec.target_atr
        else:
            stop = entry + risk_dist
            target = entry - float(f["atr"]) * spec.target_atr

        exit_price = candles[min(i + spec.max_hold_bars, len(candles) - 1)]["close"]
        exit_i = min(i + spec.max_hold_bars, len(candles) - 1)
        hit = "time"
        for j in range(i + 1, min(i + 1 + spec.max_hold_bars, len(candles))):
            bar = candles[j]
            if side == "long":
                stop_hit = bar["low"] <= stop
                target_hit = bar["high"] >= target
            else:
                stop_hit = bar["high"] >= stop
                target_hit = bar["low"] <= target
            # Pessimistic ordering if both were touched inside one candle.
            if stop_hit:
                exit_price, exit_i, hit = stop, j, "stop"
                break
            if target_hit:
                exit_price, exit_i, hit = target, j, "target"
                break

        direction = 1.0 if side == "long" else -1.0
        gross_r = ((float(exit_price) - entry) * direction) / risk_dist
        round_trip_cost = 2.0 * taker_fee_rate * entry + 2.0 * (slippage_bps / 10000.0) * entry
        cost_r = round_trip_cost / risk_dist
        net_r = gross_r - cost_r
        outcomes.append(net_r)
        if net_r > 0:
            wins += 1
        else:
            losses += 1
        i = max(i + 1, exit_i)

    if not outcomes:
        return {"strategy": spec.key, "name": spec.name, "trades": 0, "error": "no_signals"}
    gains = sum(x for x in outcomes if x > 0)
    loss_abs = abs(sum(x for x in outcomes if x < 0))
    equity = peak = max_dd = 0.0
    for x in outcomes:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    avg_r = sum(outcomes) / len(outcomes)
    return {
        "strategy": spec.key,
        "name": spec.name,
        "description": spec.description,
        "trades": len(outcomes),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(outcomes), 4),
        "expectancy_r": round(avg_r, 4),
        "net_r": round(sum(outcomes), 4),
        "profit_factor": round(gains / loss_abs, 3) if loss_abs > 0 else (999.0 if gains > 0 else 0.0),
        "max_drawdown_r": round(max_dd, 4),
        "taker_fee_rate": taker_fee_rate,
        "slippage_bps": slippage_bps,
        "candles": len(candles),
    }


def run_strategy_matrix(rows: list[list[str]], *, taker_fee_rate: float = 0.00055, slippage_bps: float = 1.0) -> list[dict[str, Any]]:
    results = [backtest_strategy(rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps) for spec in STRATEGIES]
    return sorted(
        results,
        key=lambda x: (float(x.get("expectancy_r", -999)), float(x.get("profit_factor", 0)), int(x.get("trades", 0))),
        reverse=True,
    )


def evaluate_strategy_robustness(
    rows: list[list[str]],
    spec: StrategySpec,
    *,
    taker_fee_rate: float = 0.00055,
    slippage_bps: float = 1.0,
    train_fraction: float = 0.70,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: int(float(r[0])) if r else 0)
    if len(ordered) < 400:
        base = backtest_strategy(rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
        return {"full": base, "train": {}, "out_of_sample": {}, "robust": False, "robustness_score": 0.0}
    cut = max(200, min(len(ordered) - 180, int(len(ordered) * train_fraction)))
    train_rows = list(reversed(ordered[:cut]))
    test_rows = list(reversed(ordered[cut:]))
    full = backtest_strategy(rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
    train = backtest_strategy(train_rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
    test = backtest_strategy(test_rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
    train_exp = float(train.get("expectancy_r", -999))
    test_exp = float(test.get("expectancy_r", -999))
    test_pf = float(test.get("profit_factor", 0))
    test_trades = int(test.get("trades", 0))
    robust = train_exp > 0 and test_exp > 0 and test_pf > 1.0 and test_trades >= 8
    score = 0.0
    if train_exp > 0:
        score += min(0.30, train_exp * 0.5)
    if test_exp > 0:
        score += min(0.40, test_exp * 0.7)
    if test_pf > 1:
        score += min(0.20, (test_pf - 1.0) * 0.25)
    score += min(0.10, test_trades / 200.0)
    return {"full": full, "train": train, "out_of_sample": test, "robust": robust, "robustness_score": round(max(0.0, min(1.0, score)), 3)}
