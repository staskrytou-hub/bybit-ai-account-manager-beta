from __future__ import annotations

import json
from typing import Any, Literal

from agents import Agent, ModelSettings, Runner, WebSearchTool
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from model_router import choose_model, fallback_models
from resilience import run_sync_resilient
from usage import record_usage
from agent import usage_summary


class FuturesAssessment(BaseModel):
    action: Literal["long", "short", "hold"]
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=1, max_length=1800)
    entry: float = Field(ge=0.0)
    stop_loss: float = Field(ge=0.0)
    take_profit: float = Field(ge=0.0)
    horizon: str = Field(default="intraday", max_length=120)
    catalysts: list[str] = Field(default_factory=list, max_length=8)
    invalidation: str = Field(default="", max_length=800)
    risk_notes: list[str] = Field(default_factory=list, max_length=8)
    used_news: bool = False
    regime: str = Field(default="", max_length=300)
    strategy_alignment: str = Field(default="", max_length=900)
    evidence: list[str] = Field(default_factory=list, max_length=12)


ANALYST_INSTRUCTIONS = (
    "You are Stan Futures Analyst. You analyze derivatives market context; you NEVER place orders. "
    "Use only the supplied Bybit measurements as market facts. `price` is the live ticker price, while `signal_price` is the close of the latest fully completed candle; candle-derived structural metrics use completed candles only. Do not misread a normal live-vs-signal price difference as contradictory data. If Web Search is available, use it only for relevant current macro/news catalysts and clearly distinguish external context from exchange data. "
    "Be skeptical, but do not demand perfect conditions. When a deterministic trade_proposal is supplied, your job changes from open-ended signal generation to safety verification: approve the proposal unless there is a concrete evidence-based veto. Treat local Strategy-Lab backtests as supporting or contradicting evidence, never as proof. "
    "Do not anchor decisions to RSI/EMA textbook setups; they are not strategy gates. Focus on current regime, price structure, volatility, participation, derivatives positioning, liquidity/microstructure, current catalysts, and locally falsified adaptive hypotheses. Exact output must match the schema. "
    "When trade_proposal is supplied and you approve it, return the SAME proposed direction and the SAME entry/stop_loss/take_profit geometry. Use HOLD only for a specific veto such as severe late-stage extension, direct structure/participation contradiction, invalid execution context, or materially adverse current catalyst. Do not HOLD merely because the setup is imperfect. "
    "For LONG require stop_loss < entry < take_profit. For SHORT require take_profit < entry < stop_loss. "
    "For HOLD set entry to current price and stop_loss/take_profit to 0. "
    "Do not imply guaranteed profit. Confidence should reflect evidence quality, not enthusiasm. "
    "If research_context contains promotion_alignment, treat it only as a tiny secondary tie-breaker among already valid opportunities. Never increase confidence, widen risk, raise leverage, manufacture volume, or trade solely to qualify for a reward. "
)


def analyze_snapshot(snapshot: dict[str, Any], *, include_news: bool = False, research_context: list[dict[str, Any]] | None = None, trade_proposal: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, int], str]:
    compact = {
        k: snapshot.get(k) for k in (
            "symbol","interval","price","signal_price","mark_price","spread_bps","atr_pct",
            "return_1_pct","return_4_pct","return_12_pct","return_48_pct",
            "trend_slope_20_pct","trend_slope_50_pct","realized_vol_20_pct",
            "volume_ratio_20","volume_z_20","range_position_20","range_position_50",
            "breakout_20_atr","breakdown_20_atr","vwap_distance_20_pct",
            "drawdown_from_20_high_pct","rebound_from_20_low_pct","body_strength",
            "orderbook_imbalance_10","open_interest_change_pct","oi_price_regime",
            "long_ratio","short_ratio","funding_rate","directional_score","setup_strength","local_bias",
            "ticker_24h_pct","turnover_24h","closed_candle_start_ms","forming_candle_excluded","evidence_candle_policy","evidence_model"
        )
    }
    prompt = (
        "Analyze this Bybit linear-futures snapshot for an intraday decision candidate. "
        "The deterministic risk engine will independently decide whether any candidate is allowed.\n\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + ("\n\nDETERMINISTIC TRADE PROPOSAL — review this exact geometry as APPROVE (same action/entry/SL/TP) or VETO with HOLD:\n" + json.dumps(trade_proposal or {}, ensure_ascii=False, separators=(",", ":")) if trade_proposal else "")
        + ("\n\nLOCAL RESEARCH EVIDENCE (strategy tests and optional promotion alignment; promotions never override trade quality/risk):\n" + json.dumps(research_context or [], ensure_ascii=False, separators=(",", ":")) if research_context else "")
        + ("\n\nUse web search for only high-impact current news/macro context that could materially invalidate this setup." if include_news else "\n\nDo not use web search in this run.")
    )
    setup_strength = float(snapshot.get("setup_strength", 0.0) or 0.0)
    preferred_model = "gpt-5.6-sol" if include_news and setup_strength >= 0.82 else "gpt-5.6-terra"
    reasoning_effort = "medium" if include_news else "low"
    tools = [WebSearchTool(search_context_size="low")] if include_news else []
    last: Exception | None = None
    for model in fallback_models(preferred_model):
        try:
            agent = Agent(name="Stan Futures Analyst", model=model, instructions=ANALYST_INSTRUCTIONS, output_type=FuturesAssessment, tools=tools, model_settings=ModelSettings(reasoning=Reasoning(effort=reasoning_effort), verbosity="low", max_tokens=1700))
            result = run_sync_resilient(agent, prompt, max_turns=4 if include_news else 3, kind="trading.analysis")
            usage = usage_summary(result)
            record_usage(usage)
            output = result.final_output
            if not isinstance(output, FuturesAssessment):
                raise TypeError("Trading analyst returned unexpected output type")
            return output.model_dump(), usage, model
        except Exception as exc:
            last = exc
            text = str(exc).lower()
            if not ("model" in text and any(x in text for x in ("not found","access","available","permission"))):
                raise
    raise RuntimeError(f"No configured model available for Futures Analyst: {last}")
