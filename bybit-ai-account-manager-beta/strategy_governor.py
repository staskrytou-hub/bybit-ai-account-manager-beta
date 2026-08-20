from __future__ import annotations

import json
from typing import Any

from research_store import get_research_state, set_research_state


def _candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict) or row.get("error"):
        return None
    oos = row.get("out_of_sample") or {}
    score = float(row.get("robustness_score", 0.0) or 0.0)
    exp = float(oos.get("expectancy_r", -999.0) or -999.0)
    pf = float(oos.get("profit_factor", 0.0) or 0.0)
    trades = int(oos.get("trades", 0) or 0)
    robust = bool(row.get("robust")) and score >= 0.15 and exp > 0 and pf > 1.0 and trades >= 8
    return {
        "symbol": str(row.get("symbol", "")).upper(),
        "interval": str(row.get("interval", "")),
        "strategy": str(row.get("strategy", "")),
        "name": str(row.get("name", "")),
        "family": str(row.get("strategy_family", "adaptive" if row.get("adaptive") else "benchmark")),
        "adaptive": bool(row.get("adaptive")),
        "robust": robust,
        "robustness_score": round(score, 4),
        "oos_expectancy_r": round(exp, 4),
        "oos_profit_factor": round(pf, 4),
        "oos_trades": trades,
    }


def build_strategy_governor(backtests: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [x for row in backtests if (x := _candidate(row)) is not None]
    adaptive_candidates = [x for x in candidates if x["adaptive"]]
    benchmark_candidates = [x for x in candidates if not x["adaptive"]]

    # v4.4: live strategy support is driven by CURRENT-regime adaptive hypotheses when available.
    # Legacy textbook strategies remain only as research benchmarks and never become mandatory gates.
    adaptive_approved = [x for x in adaptive_candidates if x["robust"]]
    adaptive_approved.sort(key=lambda x: (x["robustness_score"], x["oos_expectancy_r"], x["oos_profit_factor"]), reverse=True)
    benchmark_approved = [x for x in benchmark_candidates if x["robust"]]
    benchmark_approved.sort(key=lambda x: (x["robustness_score"], x["oos_expectancy_r"], x["oos_profit_factor"]), reverse=True)

    governor = {
        "approved": adaptive_approved[:16],
        "adaptive_candidate_count": len(adaptive_candidates),
        "adaptive_approved_count": len(adaptive_approved),
        "benchmark_candidate_count": len(benchmark_candidates),
        "benchmark_robust_count": len(benchmark_approved),
        "benchmark_reference": benchmark_approved[:8],
        "candidate_count": len(candidates),
        "approved_count": len(adaptive_approved),
        "policy": (
            "Live support follows current-regime adaptive hypotheses that passed local out-of-sample falsification. "
            "EMA/RSI-style benchmark systems are reference-only. Lack of an approved adaptive hypothesis raises selectivity slightly; "
            "it does not permanently prohibit a high-quality evidence-rich trade. Risk ceilings remain deterministic."
        ),
    }
    set_research_state("strategy_governor", json.dumps(governor, ensure_ascii=False))
    return governor


def current_strategy_governor() -> dict[str, Any]:
    raw = get_research_state("strategy_governor", "")
    if not raw:
        return {"approved": [], "approved_count": 0}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"approved": [], "approved_count": 0}
    except Exception:
        return {"approved": [], "approved_count": 0}


def strategy_support(symbol: str, interval: str) -> dict[str, Any]:
    gov = current_strategy_governor()
    rows = [
        x for x in list(gov.get("approved") or [])
        if str(x.get("symbol", "")).upper() == symbol.upper() and str(x.get("interval", "")) == str(interval)
    ]
    return {
        "supported": bool(rows),
        "strategies": rows[:6],
        # Small selectivity bump only; never a permanent block merely because research has not validated a hypothesis yet.
        "confidence_bump_if_unsupported": 0.02 if not rows else 0.0,
        "policy": gov.get("policy", ""),
    }
