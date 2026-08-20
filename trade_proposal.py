from __future__ import annotations

import json
import time
from typing import Any

from trading_store import get_state, set_state


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _bucket(value: Any, step: float) -> float:
    try:
        return round(round(float(value) / step) * step, 6)
    except Exception:
        return 0.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _proposal_signature(snapshot: dict[str, Any], action: str) -> str:
    return ":".join([
        str(snapshot.get("symbol", "")).upper(),
        str(action).lower(),
        f"setup={_bucket(snapshot.get('setup_strength', 0), 0.08):.2f}",
        f"dir={_bucket(snapshot.get('directional_score', 0), 0.20):.2f}",
        f"r4={_bucket(snapshot.get('return_4_pct', 0), 1.0):.1f}",
        f"vwap={_bucket(snapshot.get('vwap_distance_20_pct', 0), 1.0):.1f}",
        f"range={_bucket(snapshot.get('range_position_20', 0.5), 0.15):.2f}",
        f"oi={str(snapshot.get('oi_price_regime', 'na'))}",
        f"imb={_bucket(snapshot.get('orderbook_imbalance_10', 0), 0.20):.2f}",
    ])




def choose_best_preflight_candidate(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the best already-built executable proposal without spending AI tokens."""
    valid = [r for r in rows if bool((r.get("proposal") or {}).get("eligible")) and not bool(r.get("veto_blocked"))]
    if not valid:
        return None
    def _score(row: dict[str, Any]) -> float:
        proposal = row.get("proposal") or {}
        quality = float(proposal.get("quality", 0.0) or 0.0)
        priority_bonus = 0.08 if str(proposal.get("priority", "normal")) == "high" else 0.0
        scanner_bonus = 0.05 * float(row.get("scanner_score", 0.0) or 0.0)
        return quality + priority_bonus + scanner_bonus
    return max(valid, key=_score)

def _veto_key(symbol: str, interval: str, lane: str) -> str:
    return f"proposal_veto:v464:{lane}:{str(symbol).upper()}:{interval}"


def proposal_veto_status(symbol: str, interval: str, lane: str = "futures") -> dict[str, Any]:
    raw = get_state(_veto_key(symbol, interval, lane), "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_proposal_veto(
    symbol: str,
    interval: str,
    *,
    signature: str,
    reason: str,
    action: str,
    minutes: int = 90,
    lane: str = "futures",
) -> None:
    payload = {
        "signature": str(signature or "")[:500],
        "reason": str(reason or "AI veto/HOLD")[:1000],
        "action": str(action or "")[:20],
        "until_ts": time.time() + max(15, int(minutes or 90)) * 60,
        "recorded_ts": time.time(),
    }
    set_state(_veto_key(symbol, interval, lane), json.dumps(payload, ensure_ascii=False))


def clear_proposal_veto(symbol: str, interval: str, lane: str = "futures") -> None:
    set_state(_veto_key(symbol, interval, lane), "")


def _approval_key(symbol: str, interval: str, lane: str) -> str:
    return f"proposal_approval:v464:{lane}:{str(symbol).upper()}:{interval}"


def record_proposal_approval(
    symbol: str, interval: str, *, signature: str, action: str, confidence: float,
    model: str = "", minutes: int = 45, lane: str = "futures",
) -> None:
    payload = {
        "signature": str(signature or "")[:500],
        "action": str(action or "")[:20],
        "confidence": float(confidence or 0.0),
        "model": str(model or "")[:120],
        "until_ts": time.time() + max(10, int(minutes or 45)) * 60,
        "recorded_ts": time.time(),
    }
    set_state(_approval_key(symbol, interval, lane), json.dumps(payload, ensure_ascii=False))


def proposal_approval_status(symbol: str, interval: str, lane: str = "futures") -> dict[str, Any]:
    raw = get_state(_approval_key(symbol, interval, lane), "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def reusable_proposal_approval(proposal: dict[str, Any], interval: str, lane: str = "futures") -> dict[str, Any]:
    """Return a still-valid prior APPROVE for the same structural proposal signature.

    This is deliberately narrower than candle identity: if structure/action changes enough to
    change the quantized signature, a new paid verification is required.
    """
    if not bool(proposal.get("eligible")):
        return {}
    state = proposal_approval_status(str(proposal.get("symbol", "")), interval, lane)
    if not state or float(state.get("until_ts", 0.0) or 0.0) <= time.time():
        return {}
    if str(state.get("signature", "")) != str(proposal.get("signature", "")):
        return {}
    if str(state.get("action", "")).lower() != str(proposal.get("action", "")).lower():
        return {}
    return state


def clear_proposal_approval(symbol: str, interval: str, lane: str = "futures") -> None:
    set_state(_approval_key(symbol, interval, lane), "")


def veto_blocks_proposal(proposal: dict[str, Any], interval: str, lane: str = "futures") -> tuple[bool, str]:
    symbol = str(proposal.get("symbol", "")).upper()
    state = proposal_veto_status(symbol, interval, lane)
    if not state:
        return False, ""
    if float(state.get("until_ts", 0.0) or 0.0) <= time.time():
        return False, ""
    if str(state.get("signature", "")) != str(proposal.get("signature", "")):
        # Structural evidence changed enough to create a different proposal. Do not let a
        # stale HOLD suppress a genuinely new regime/action.
        return False, ""
    remaining = max(0, int((float(state.get("until_ts", 0.0)) - time.time()) / 60))
    return True, f"AI veto memory active for same proposal evidence (~{remaining}m remaining)"


def _stats_key(lane: str) -> str:
    return f"trade_proposal_stats:v464:{lane}"


def proposal_stats(lane: str = "futures") -> dict[str, Any]:
    raw = get_state(_stats_key(lane), "")
    if not raw:
        return {
            "created": 0,
            "ai_verified": 0,
            "ai_approved": 0,
            "ai_reused": 0,
            "ai_vetoed": 0,
            "risk_passed": 0,
            "capacity_resized": 0,
            "capacity_rejected": 0,
            "submitted": 0,
            "confirmed": 0,
            "execution_failed": 0,
            "execution_uncertain": 0,
            "executed": 0,
            "last_event": "",
            "last_symbol": "",
            "last_ts": 0.0,
        }
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def bump_proposal_stat(event: str, *, lane: str = "futures", symbol: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = proposal_stats(lane)
    if event in {
        "created", "ai_verified", "ai_approved", "ai_reused", "ai_vetoed", "risk_passed",
        "capacity_resized", "capacity_rejected", "submitted", "confirmed", "execution_failed", "execution_uncertain", "executed",
    }:
        data[event] = int(data.get(event, 0) or 0) + 1
    data["last_event"] = str(event)
    data["last_symbol"] = str(symbol or "").upper()
    data["last_ts"] = time.time()
    if extra:
        data["last_extra"] = dict(extra)
    set_state(_stats_key(lane), json.dumps(data, ensure_ascii=False)[:12000])
    return data


def build_futures_proposal(
    snapshot: dict[str, Any],
    cfg: dict[str, Any],
    *,
    strategy_supported: bool = False,
) -> dict[str, Any]:
    """Build a deterministic executable geometry before paid AI.

    v4.6.3 changes AI from an open-ended signal generator into a safety verifier. The
    proposal engine is intentionally conservative about late-stage chases, but it does not
    require textbook EMA/RSI conditions. It focuses on direction, structure, participation,
    execution quality and a concrete ATR-based stop/target geometry.
    """
    symbol = str(snapshot.get("symbol", "")).upper()
    interval = str(snapshot.get("interval", "15"))
    setup = _f(snapshot.get("setup_strength"))
    direction = _f(snapshot.get("directional_score"))
    bias = str(snapshot.get("local_bias", "neutral")).lower()
    price = _f(snapshot.get("price"))
    signal_price = _f(snapshot.get("signal_price"), price)
    atr = _f(snapshot.get("atr14"))
    atr_pct = _f(snapshot.get("atr_pct"))
    spread = _f(snapshot.get("spread_bps"), 999.0)
    max_spread = _f(cfg.get("max_spread_bps"), 12.0)
    r4 = _f(snapshot.get("return_4_pct"))
    r12 = _f(snapshot.get("return_12_pct"))
    vwap = _f(snapshot.get("vwap_distance_20_pct"))
    range20 = _f(snapshot.get("range_position_20"), 0.5)
    breakout = _f(snapshot.get("breakout_20_atr"))
    breakdown = _f(snapshot.get("breakdown_20_atr"))
    volume_ratio = _f(snapshot.get("volume_ratio_20"), 1.0)
    volume_z = _f(snapshot.get("volume_z_20"))
    imbalance = _f(snapshot.get("orderbook_imbalance_10"))
    oi_change = _f(snapshot.get("open_interest_change_pct"))
    oi_regime = str(snapshot.get("oi_price_regime", "flat"))
    rsi = _f(snapshot.get("rsi14"), 50.0)

    base = {
        "eligible": False,
        "symbol": symbol,
        "interval": interval,
        "action": "hold",
        "entry": price,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "reward_risk": 0.0,
        "quality": 0.0,
        "priority": "none",
        "reason": "",
        "signature": "",
        "strategy_supported": bool(strategy_supported),
        "factors": [],
    }
    if price <= 0 or atr <= 0:
        base["reason"] = "proposal unavailable: invalid live price/ATR"
        return base
    if spread > max_spread:
        base["reason"] = f"proposal blocked: spread {spread:.2f} bps > {max_spread:.2f}"
        return base

    min_setup = _f(cfg.get("proposal_min_setup", 0.58))
    min_direction = _f(cfg.get("proposal_min_direction", 0.26))
    if strategy_supported:
        min_setup = max(0.45, min_setup - 0.05)
        min_direction = max(0.18, min_direction - 0.04)

    action = "long" if bias == "long" and direction >= min_direction else ("short" if bias == "short" and direction <= -min_direction else "hold")
    if action == "hold":
        base["reason"] = f"proposal prefilter: directional conviction {direction:.2f} below executable threshold"
        return base
    if setup < min_setup:
        base["reason"] = f"proposal prefilter: setup {setup:.2f} < {min_setup:.2f}"
        return base

    # Late-stage extension is the dominant pattern behind the observed repeated HOLDs.
    # Reject it locally instead of paying AI to say the same thing again.
    extension_limit = max(1.20, atr_pct * _f(cfg.get("proposal_max_vwap_atr_multiple", 1.75)))
    if action == "long" and vwap > extension_limit and rsi >= 78:
        base["reason"] = f"proposal prefilter: long chase too extended ({vwap:.2f}% above VWAP, RSI {rsi:.0f})"
        return base
    if action == "short" and vwap < -extension_limit and rsi <= 22:
        base["reason"] = f"proposal prefilter: short chase too extended ({vwap:.2f}% below VWAP, RSI {rsi:.0f})"
        return base
    if action == "long" and range20 > 1.10 and volume_ratio < 1.15:
        base["reason"] = "proposal prefilter: unconfirmed long breakout above recent range"
        return base
    if action == "short" and range20 < -0.10 and volume_ratio < 1.15:
        base["reason"] = "proposal prefilter: unconfirmed short breakdown below recent range"
        return base

    aligned_oi = (action == "long" and oi_regime == "price_up_oi_up") or (action == "short" and oi_regime == "price_down_oi_up")
    opposing_oi = (action == "long" and oi_regime == "price_up_oi_down") or (action == "short" and oi_regime == "price_down_oi_down")
    micro_aligned = imbalance > 0.08 if action == "long" else imbalance < -0.08
    momentum_aligned = r4 > 0 if action == "long" else r4 < 0
    medium_aligned = r12 > 0 if action == "long" else r12 < 0
    breakout_aligned = breakout >= -0.30 if action == "long" else breakdown >= -0.30

    participation = _clamp((volume_ratio - 0.55) / 1.45) * 0.45 + _clamp(max(volume_z, 0.0) / 3.0) * 0.15
    participation += 0.25 if aligned_oi else (0.04 if opposing_oi else 0.12)
    participation += 0.15 if micro_aligned else 0.0
    participation = _clamp(participation)

    # Reward fresh structure and pullback/retest locations; penalize late chases.
    if action == "long":
        location = 1.0 - _clamp(max(0.0, range20 - 0.78) / 0.42)
        if -0.60 <= vwap <= max(1.0, atr_pct * 0.90):
            location = min(1.0, location + 0.18)
    else:
        location = 1.0 - _clamp(max(0.0, 0.22 - range20) / 0.42)
        if min(-1.0, -atr_pct * 0.90) <= vwap <= 0.60:
            location = min(1.0, location + 0.18)
    location = _clamp(location)

    structure_bonus = (0.08 if momentum_aligned else 0.0) + (0.05 if medium_aligned else 0.0) + (0.07 if breakout_aligned else 0.0)
    quality = 0.47 * setup + 0.20 * _clamp(abs(direction)) + 0.18 * participation + 0.15 * location + structure_bonus
    if opposing_oi:
        quality -= 0.07
    if abs(price - signal_price) / max(price, 1e-12) > max(0.012, atr_pct / 100.0 * 0.75):
        quality -= 0.06
    quality = _clamp(quality)

    min_quality = _f(cfg.get("proposal_min_quality", 0.62)) - (0.04 if strategy_supported else 0.0)
    if quality < min_quality:
        base["reason"] = f"proposal quality {quality:.2f} < {min_quality:.2f}"
        base["quality"] = round(quality, 4)
        return base

    stop_atr = _f(cfg.get("proposal_stop_atr", 0.90))
    rr = max(_f(cfg.get("min_reward_risk", 1.30)), _f(cfg.get("proposal_target_rr", 1.65)))
    stop_distance = max(atr * stop_atr, price * 0.0030)
    if action == "long":
        stop = price - stop_distance
        target = price + stop_distance * rr
    else:
        stop = price + stop_distance
        target = price - stop_distance * rr
    if stop <= 0 or target <= 0:
        base["reason"] = "proposal geometry invalid"
        return base

    priority_threshold = _f(cfg.get("proposal_high_priority_quality", 0.76))
    priority = "high" if quality >= priority_threshold or (strategy_supported and quality >= priority_threshold - 0.05) else "normal"
    signature = _proposal_signature(snapshot, action)
    factors = [
        f"setup={setup:.2f}", f"direction={direction:.2f}", f"participation={participation:.2f}",
        f"location={location:.2f}", f"vwap={vwap:.2f}%", f"OI={oi_regime}", f"spread={spread:.2f}bps",
    ]
    return {
        **base,
        "eligible": True,
        "action": action,
        "entry": round(price, 12),
        "stop_loss": round(stop, 12),
        "take_profit": round(target, 12),
        "reward_risk": round(rr, 3),
        "quality": round(quality, 4),
        "priority": priority,
        "reason": "deterministic executable proposal ready for AI safety verification",
        "signature": signature,
        "strategy_supported": bool(strategy_supported),
        "factors": factors,
    }


def build_spot_proposal(
    snapshot: dict[str, Any],
    cfg: dict[str, Any],
    *,
    strategy_supported: bool = False,
) -> dict[str, Any]:
    symbol = str(snapshot.get("symbol", "")).upper()
    interval = str(snapshot.get("interval", "15"))
    setup = _f(snapshot.get("setup_strength"))
    price = _f(snapshot.get("price"))
    atr = _f(snapshot.get("atr14"))
    atr_pct = _f(snapshot.get("atr_pct"))
    vwap = _f(snapshot.get("vwap_distance_20_pct"))
    range20 = _f(snapshot.get("range_position_20"), 0.5)
    r4 = _f(snapshot.get("return_4_pct"))
    r12 = _f(snapshot.get("return_12_pct"))
    volume_ratio = _f(snapshot.get("volume_ratio_20"), 1.0)
    imbalance = _f(snapshot.get("orderbook_imbalance_10"))
    spread = _f(snapshot.get("spread_bps"), 999.0)
    max_spread = _f(cfg.get("spot_max_spread_bps"), 22.0)
    base = {
        "eligible": False, "symbol": symbol, "interval": interval, "action": "hold",
        "entry": price, "stop_loss": 0.0, "take_profit": 0.0, "reward_risk": 0.0,
        "quality": 0.0, "priority": "none", "reason": "", "signature": "",
        "strategy_supported": bool(strategy_supported), "factors": [],
    }
    if price <= 0 or atr <= 0:
        base["reason"] = "spot proposal unavailable: invalid live price/ATR"
        return base
    if spread > max_spread:
        base["reason"] = f"spot proposal blocked: spread {spread:.2f} bps > {max_spread:.2f}"
        return base
    if str(snapshot.get("local_bias", "")).lower() != "buy_candidate":
        base["reason"] = "spot proposal prefilter: no buy_candidate bias"
        return base
    min_setup = _f(cfg.get("spot_proposal_min_setup", 0.64)) - (0.06 if strategy_supported else 0.0)
    if setup < min_setup:
        base["reason"] = f"spot proposal prefilter: setup {setup:.2f} < {min_setup:.2f}"
        return base
    extension_limit = max(1.0, atr_pct * _f(cfg.get("spot_proposal_max_vwap_atr_multiple", 1.55)))
    if vwap > extension_limit or range20 > 1.08:
        base["reason"] = f"spot proposal prefilter: entry too extended (VWAP {vwap:.2f}%, range {range20:.2f})"
        return base

    participation = _clamp((volume_ratio - 0.55) / 1.45) * 0.65 + (0.20 if imbalance > 0.05 else 0.05)
    momentum = 0.12 if r4 > 0 else 0.0
    medium = 0.08 if r12 > 0 else 0.0
    location = 1.0 - _clamp(max(0.0, range20 - 0.75) / 0.45)
    quality = _clamp(0.55 * setup + 0.20 * participation + 0.15 * location + momentum + medium)
    min_quality = _f(cfg.get("spot_proposal_min_quality", 0.66)) - (0.05 if strategy_supported else 0.0)
    if quality < min_quality:
        base["reason"] = f"spot proposal quality {quality:.2f} < {min_quality:.2f}"
        base["quality"] = round(quality, 4)
        return base

    stop_distance = max(atr * _f(cfg.get("spot_proposal_stop_atr", 0.85)), price * 0.004)
    rr = max(_f(cfg.get("spot_min_reward_risk", 1.35)), _f(cfg.get("spot_proposal_target_rr", 1.60)))
    stop = price - stop_distance
    target = price + stop_distance * rr
    if stop <= 0:
        base["reason"] = "spot proposal geometry invalid"
        return base
    signature = ":".join([
        symbol, "buy", f"setup={_bucket(setup,0.08):.2f}", f"r4={_bucket(r4,1.0):.1f}",
        f"vwap={_bucket(vwap,1.0):.1f}", f"range={_bucket(range20,0.15):.2f}",
        f"vol={_bucket(volume_ratio,0.25):.2f}", f"imb={_bucket(imbalance,0.20):.2f}",
    ])
    priority_threshold = _f(cfg.get("spot_proposal_high_priority_quality", 0.78))
    priority = "high" if quality >= priority_threshold or (strategy_supported and quality >= priority_threshold - 0.05) else "normal"
    return {
        **base, "eligible": True, "action": "buy", "entry": round(price, 12),
        "stop_loss": round(stop, 12), "take_profit": round(target, 12), "reward_risk": round(rr, 3),
        "quality": round(quality, 4), "priority": priority,
        "reason": "deterministic Spot proposal ready for AI safety verification", "signature": signature,
        "factors": [f"setup={setup:.2f}", f"participation={participation:.2f}", f"location={location:.2f}", f"vwap={vwap:.2f}%", f"spread={spread:.2f}bps"],
    }
