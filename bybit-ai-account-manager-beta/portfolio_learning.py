from __future__ import annotations

import math
from typing import Any

from bybit_client import BybitClient


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _returns(rows: list[list[str]]) -> list[float]:
    ordered = sorted(rows, key=lambda r: int(float(r[0])) if r else 0)
    closes: list[float] = []
    for row in ordered:
        try:
            closes.append(float(row[4]))
        except Exception:
            pass
    out: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            out.append(closes[i] / closes[i - 1] - 1.0)
    return out


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 20:
        return 0.0
    a = a[-n:]
    b = b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 1e-18 or vb <= 1e-18:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return max(-1.0, min(1.0, cov / math.sqrt(va * vb)))


def symbol_correlation(symbol_a: str, symbol_b: str, *, interval: str = "15", testnet: bool = False, limit: int = 120) -> float:
    if symbol_a.upper() == symbol_b.upper():
        return 1.0
    client = BybitClient(testnet=testnet)
    a = _returns(client.get_kline(symbol_a, interval=interval, limit=limit, category="linear"))
    b = _returns(client.get_kline(symbol_b, interval=interval, limit=limit, category="linear"))
    return _corr(a, b)


def portfolio_state(
    client: BybitClient,
    *,
    candidate_symbol: str,
    candidate_action: str,
    equity: float,
    interval: str = "15",
    correlation_threshold: float = 0.85,
) -> dict[str, Any]:
    """Measure existing live derivatives risk before adding another learning position.

    This is intentionally deterministic and token-free. Risk is estimated from each open
    position's actual Bybit avgPrice/size/stopLoss. Missing protection is surfaced rather
    than silently assuming zero risk.
    """
    try:
        positions = [p for p in client.get_positions(settle_coin="USDT") if _f(p.get("size")) > 0]
    except Exception:
        positions = []

    eq = max(float(equity or 0.0), 1e-9)
    risk_cash = 0.0
    unprotected: list[str] = []
    same_symbol = False
    correlations: list[dict[str, Any]] = []
    candidate_dir = 1 if str(candidate_action).lower() == "long" else (-1 if str(candidate_action).lower() == "short" else 0)

    for p in positions:
        symbol = str(p.get("symbol") or "").upper()
        if not symbol:
            continue
        if symbol == candidate_symbol.upper():
            same_symbol = True
        avg = _f(p.get("avgPrice"))
        size = _f(p.get("size"))
        stop = _f(p.get("stopLoss"))
        if avg > 0 and size > 0 and stop > 0:
            risk_cash += abs(avg - stop) * size
        else:
            unprotected.append(symbol)

        if candidate_dir and symbol != candidate_symbol.upper():
            try:
                corr = symbol_correlation(candidate_symbol, symbol, interval=interval, testnet=client.testnet)
            except Exception:
                corr = 0.0
            side = str(p.get("side") or "").lower()
            existing_dir = 1 if side == "buy" else (-1 if side == "sell" else 0)
            directional_overlap = corr * candidate_dir * existing_dir
            correlations.append({
                "symbol": symbol,
                "correlation": round(corr, 4),
                "directional_overlap": round(directional_overlap, 4),
                "too_correlated": directional_overlap >= float(correlation_threshold),
            })

    correlations.sort(key=lambda x: float(x.get("directional_overlap", 0.0)), reverse=True)
    return {
        "open_positions": len(positions),
        "symbols": [str(p.get("symbol") or "").upper() for p in positions],
        "same_symbol_open": same_symbol,
        "estimated_open_risk_cash": round(risk_cash, 6),
        "estimated_open_risk_pct": round(risk_cash / eq * 100.0, 5),
        "unprotected_positions": sorted(set(unprotected)),
        "max_directional_correlation": correlations[0] if correlations else {},
        "correlations": correlations[:6],
    }
