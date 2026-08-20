from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, asdict
from typing import Any

from market_analysis import atr, parse_klines
from research_store import get_research_state, set_research_state

ALLOWED_FEATURES = {
    "return_1_pct", "return_4_pct", "return_12_pct", "return_48_pct",
    "trend_slope_20_pct", "trend_slope_50_pct", "realized_vol_20_pct",
    "atr_pct", "volume_z_20", "range_position_20", "range_position_50",
    "breakout_20_atr", "breakdown_20_atr", "vwap_distance_20_pct",
    "drawdown_from_20_high_pct", "rebound_from_20_low_pct", "body_strength",
}
ALLOWED_OPS = {">", ">=", "<", "<=", "abs>"}


@dataclass(frozen=True)
class Rule:
    feature: str
    op: str
    value: float


@dataclass(frozen=True)
class AdaptiveStrategySpec:
    key: str
    name: str
    thesis: str
    long_all: tuple[Rule, ...]
    short_all: tuple[Rule, ...]
    stop_atr: float
    target_atr: float
    max_hold_bars: int
    source_context: str = ""


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def _slope_pct(values: list[float]) -> float:
    if len(values) < 3 or values[0] == 0:
        return 0.0
    n = len(values)
    sx = n * (n - 1) / 2.0
    sy = sum(values)
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sxy = sum(i * v for i, v in enumerate(values))
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / den if den else 0.0
    return slope / max(abs(values[-1]), 1e-12) * 100.0


def historical_features(candles: list[dict[str, float]], i: int) -> dict[str, float]:
    start = max(0, i - 80)
    sample = candles[start:i + 1]
    closes = [float(x["close"]) for x in sample]
    vols = [float(x["volume"]) for x in sample]
    if len(closes) < 55:
        return {}
    close = closes[-1]
    a14 = atr(sample, 14)
    rets = []
    for j in range(1, len(closes)):
        prev = closes[j - 1]
        rets.append((closes[j] / prev - 1.0) * 100.0 if prev else 0.0)
    prior20 = candles[max(0, i - 20):i]
    prior50 = candles[max(0, i - 50):i]
    hi20 = max((float(x["high"]) for x in prior20), default=close)
    lo20 = min((float(x["low"]) for x in prior20), default=close)
    hi50 = max((float(x["high"]) for x in prior50), default=close)
    lo50 = min((float(x["low"]) for x in prior50), default=close)
    rng20 = max(hi20 - lo20, 1e-12)
    rng50 = max(hi50 - lo50, 1e-12)
    vol20 = vols[-20:]
    vol_base = vol20[:-1] if len(vol20) > 1 else vol20
    vol_mean = _mean(vol_base)
    vol_std = _std(vol_base)
    vwap_num = sum(c * v for c, v in zip(closes[-20:], vols[-20:]))
    vwap_den = sum(vols[-20:]) or 1.0
    vwap20 = vwap_num / vwap_den
    o = float(candles[i]["open"])
    h = float(candles[i]["high"])
    l = float(candles[i]["low"])
    body_strength = abs(close - o) / max(h - l, 1e-12)

    def ret(n: int) -> float:
        if len(closes) <= n or closes[-1 - n] == 0:
            return 0.0
        return (close / closes[-1 - n] - 1.0) * 100.0

    return {
        "return_1_pct": ret(1),
        "return_4_pct": ret(4),
        "return_12_pct": ret(12),
        "return_48_pct": ret(48),
        "trend_slope_20_pct": _slope_pct(closes[-20:]),
        "trend_slope_50_pct": _slope_pct(closes[-50:]),
        "realized_vol_20_pct": _std(rets[-20:]),
        "atr_pct": (a14 / close * 100.0) if close else 0.0,
        "volume_z_20": ((vols[-1] - vol_mean) / vol_std) if vol_std > 1e-12 else 0.0,
        "range_position_20": (close - lo20) / rng20,
        "range_position_50": (close - lo50) / rng50,
        "breakout_20_atr": (close - hi20) / max(a14, 1e-12),
        "breakdown_20_atr": (lo20 - close) / max(a14, 1e-12),
        "vwap_distance_20_pct": ((close / vwap20) - 1.0) * 100.0 if vwap20 else 0.0,
        "drawdown_from_20_high_pct": ((close / hi20) - 1.0) * 100.0 if hi20 else 0.0,
        "rebound_from_20_low_pct": ((close / lo20) - 1.0) * 100.0 if lo20 else 0.0,
        "body_strength": body_strength,
    }


def _rule_ok(rule: Rule, f: dict[str, float]) -> bool:
    value = float(f.get(rule.feature, 0.0))
    target = float(rule.value)
    if rule.op == ">": return value > target
    if rule.op == ">=": return value >= target
    if rule.op == "<": return value < target
    if rule.op == "<=": return value <= target
    if rule.op == "abs>": return abs(value) > target
    return False


def _signal(spec: AdaptiveStrategySpec, f: dict[str, float]) -> str:
    long_ok = bool(spec.long_all) and all(_rule_ok(r, f) for r in spec.long_all)
    short_ok = bool(spec.short_all) and all(_rule_ok(r, f) for r in spec.short_all)
    if long_ok and not short_ok: return "long"
    if short_ok and not long_ok: return "short"
    return ""


def validate_spec(data: dict[str, Any], index: int = 0) -> AdaptiveStrategySpec | None:
    def rules(name: str) -> tuple[Rule, ...]:
        out: list[Rule] = []
        for row in list(data.get(name) or [])[:6]:
            if not isinstance(row, dict):
                continue
            feature = str(row.get("feature", ""))
            op = str(row.get("op", ""))
            if feature not in ALLOWED_FEATURES or op not in ALLOWED_OPS:
                continue
            try:
                value = float(row.get("value"))
            except Exception:
                continue
            if not math.isfinite(value):
                continue
            out.append(Rule(feature, op, value))
        return tuple(out)

    long_rules = rules("long_all")
    short_rules = rules("short_all")
    if not long_rules and not short_rules:
        return None
    try:
        stop_atr = max(0.4, min(float(data.get("stop_atr", 1.4)), 4.0))
        target_atr = max(stop_atr * 1.15, min(float(data.get("target_atr", 2.2)), 8.0))
        hold = max(2, min(int(data.get("max_hold_bars", 16)), 96))
    except Exception:
        return None
    name = str(data.get("name") or f"Adaptive hypothesis {index + 1}")[:120]
    key = str(data.get("key") or f"adaptive_{index + 1}").lower().replace(" ", "_")[:80]
    return AdaptiveStrategySpec(
        key=key,
        name=name,
        thesis=str(data.get("thesis") or "Current-regime hypothesis")[:800],
        long_all=long_rules,
        short_all=short_rules,
        stop_atr=stop_atr,
        target_atr=target_atr,
        max_hold_bars=hold,
        source_context=str(data.get("source_context") or "")[:600],
    )


def spec_to_dict(spec: AdaptiveStrategySpec) -> dict[str, Any]:
    return {
        "key": spec.key,
        "name": spec.name,
        "thesis": spec.thesis,
        "long_all": [asdict(x) for x in spec.long_all],
        "short_all": [asdict(x) for x in spec.short_all],
        "stop_atr": spec.stop_atr,
        "target_atr": spec.target_atr,
        "max_hold_bars": spec.max_hold_bars,
        "source_context": spec.source_context,
    }


def backtest_adaptive_strategy(rows: list[list[str]], spec: AdaptiveStrategySpec, *, taker_fee_rate: float = 0.00055, slippage_bps: float = 1.5) -> dict[str, Any]:
    candles = parse_klines(rows)
    if len(candles) < 220:
        return {"strategy": spec.key, "name": spec.name, "trades": 0, "error": "insufficient_history"}
    outcomes: list[float] = []
    wins = 0
    i = 80
    while i < len(candles) - 2:
        f = historical_features(candles, i)
        side = _signal(spec, f) if f else ""
        if not side:
            i += 1
            continue
        next_bar = candles[i + 1]
        entry = float(next_bar["open"])
        a = atr(candles[max(0, i - 80):i + 1], 14)
        risk_dist = a * spec.stop_atr
        if entry <= 0 or risk_dist <= 0:
            i += 1
            continue
        if side == "long":
            stop, target = entry - risk_dist, entry + a * spec.target_atr
        else:
            stop, target = entry + risk_dist, entry - a * spec.target_atr
        exit_i = min(i + spec.max_hold_bars, len(candles) - 1)
        exit_price = float(candles[exit_i]["close"])
        for j in range(i + 1, min(i + 1 + spec.max_hold_bars, len(candles))):
            bar = candles[j]
            stop_hit = float(bar["low"]) <= stop if side == "long" else float(bar["high"]) >= stop
            target_hit = float(bar["high"]) >= target if side == "long" else float(bar["low"]) <= target
            if stop_hit:
                exit_i, exit_price = j, stop
                break
            if target_hit:
                exit_i, exit_price = j, target
                break
        direction = 1.0 if side == "long" else -1.0
        gross_r = ((exit_price - entry) * direction) / risk_dist
        round_trip_cost = 2.0 * taker_fee_rate * entry + 2.0 * (slippage_bps / 10000.0) * entry
        net_r = gross_r - round_trip_cost / risk_dist
        outcomes.append(net_r)
        if net_r > 0: wins += 1
        i = max(i + 1, exit_i)
    if not outcomes:
        return {"strategy": spec.key, "name": spec.name, "trades": 0, "error": "no_signals"}
    gains = sum(x for x in outcomes if x > 0)
    losses = abs(sum(x for x in outcomes if x < 0))
    curve = peak = max_dd = 0.0
    for x in outcomes:
        curve += x
        peak = max(peak, curve)
        max_dd = max(max_dd, peak - curve)
    return {
        "strategy": spec.key, "name": spec.name, "description": spec.thesis,
        "trades": len(outcomes), "wins": wins, "losses": len(outcomes) - wins,
        "win_rate": round(wins / len(outcomes), 4), "expectancy_r": round(sum(outcomes) / len(outcomes), 4),
        "net_r": round(sum(outcomes), 4), "profit_factor": round(gains / losses, 3) if losses > 0 else (999.0 if gains > 0 else 0.0),
        "max_drawdown_r": round(max_dd, 4), "taker_fee_rate": taker_fee_rate, "slippage_bps": slippage_bps,
        "candles": len(candles), "adaptive": True,
    }


def evaluate_adaptive_robustness(rows: list[list[str]], spec: AdaptiveStrategySpec, *, taker_fee_rate: float = 0.00055, slippage_bps: float = 1.5, train_fraction: float = 0.70) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: int(float(r[0])) if r else 0)
    if len(ordered) < 500:
        full = backtest_adaptive_strategy(rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
        return {"full": full, "train": {}, "out_of_sample": {}, "robust": False, "robustness_score": 0.0}
    cut = max(260, min(len(ordered) - 220, int(len(ordered) * train_fraction)))
    train_rows = list(reversed(ordered[:cut]))
    test_rows = list(reversed(ordered[cut:]))
    full = backtest_adaptive_strategy(rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
    train = backtest_adaptive_strategy(train_rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
    test = backtest_adaptive_strategy(test_rows, spec, taker_fee_rate=taker_fee_rate, slippage_bps=slippage_bps)
    train_exp = float(train.get("expectancy_r", -999) or -999)
    test_exp = float(test.get("expectancy_r", -999) or -999)
    test_pf = float(test.get("profit_factor", 0) or 0)
    test_trades = int(test.get("trades", 0) or 0)
    robust = train_exp > 0 and test_exp > 0 and test_pf > 1.0 and test_trades >= 8
    score = 0.0
    if train_exp > 0: score += min(0.25, train_exp * 0.45)
    if test_exp > 0: score += min(0.40, test_exp * 0.70)
    if test_pf > 1: score += min(0.20, (test_pf - 1.0) * 0.25)
    score += min(0.15, test_trades / 160.0)
    return {"full": full, "train": train, "out_of_sample": test, "robust": robust, "robustness_score": round(max(0.0, min(1.0, score)), 3)}


def store_adaptive_specs(specs: list[AdaptiveStrategySpec]) -> None:
    set_research_state("adaptive_strategy_specs", json.dumps([spec_to_dict(x) for x in specs], ensure_ascii=False))


def load_adaptive_specs() -> list[AdaptiveStrategySpec]:
    raw = get_research_state("adaptive_strategy_specs", "")
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except Exception:
        return []
    out: list[AdaptiveStrategySpec] = []
    for i, row in enumerate(rows if isinstance(rows, list) else []):
        if isinstance(row, dict):
            spec = validate_spec(row, i)
            if spec: out.append(spec)
    return out


def snapshot_feature_view(snapshot: dict[str, Any]) -> dict[str, float]:
    return {feature: float(snapshot.get(feature, 0.0) or 0.0) for feature in ALLOWED_FEATURES}


def live_adaptive_matches(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    f = snapshot_feature_view(snapshot)
    out: list[dict[str, Any]] = []
    for spec in load_adaptive_specs():
        side = _signal(spec, f)
        if side:
            out.append({"key": spec.key, "name": spec.name, "side": side, "thesis": spec.thesis})
    return out[:8]
