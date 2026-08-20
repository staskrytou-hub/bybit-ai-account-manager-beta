from __future__ import annotations

import json
from typing import Any, Literal

from agents import Agent, ModelSettings, Runner, WebSearchTool
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from agent import usage_summary
from model_router import fallback_models
from resilience import run_sync_resilient
from trading_config import load_trading_settings
from trading_usage import record_trading_tokens, trading_tokens_today


class SpotAssessment(BaseModel):
    action: Literal["buy", "hold"]
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=1, max_length=1800)
    entry: float = Field(ge=0.0)
    stop_loss: float = Field(ge=0.0)
    take_profit: float = Field(ge=0.0)
    horizon: str = Field(default="intraday", max_length=120)
    regime: str = Field(default="", max_length=300)
    catalysts: list[str] = Field(default_factory=list, max_length=8)
    invalidation: str = Field(default="", max_length=800)
    risk_notes: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    used_news: bool = False


INSTRUCTIONS = (
    "You are Stan Spot Opportunity Analyst. You analyze a non-margin Bybit Spot candidate; you NEVER place orders. "
    "Do not use RSI or EMA as mandatory strategy gates. Treat every supplied measurement as evidence, not a textbook signal. `price` is the live ticker price, while `signal_price` is the close of the latest fully completed candle; candle-derived structural metrics use completed candles only. "
    "Focus on current market regime, price structure, liquidity, volatility, participation, order-book imbalance, event/news catalysts, "
    "and locally tested strategy hypotheses. Use web search only when explicitly enabled and prefer official/primary/reputable sources. "
    "Social posts are hypothesis inspiration, not facts. When a deterministic trade_proposal is supplied, act as a safety verifier rather than an open-ended signal generator: approve the exact proposal unless there is a concrete evidence-based veto. "
    "Spot in this module is unleveraged: only BUY or HOLD. When trade_proposal is supplied and you approve it, return the SAME proposed entry/stop_loss/take_profit. Use HOLD only for a specific veto such as severe extension, weak/contradictory participation, invalid execution context, or a materially adverse catalyst; do not require perfect conditions. For BUY require stop_loss < entry < take_profit. "
    "For HOLD set entry to current price and stop_loss/take_profit to 0. Do not imply guaranteed profit. "
    "Promotions/events may be a secondary expected-value benefit only after an independently valid trade exists; never manufacture volume."
)


def analyze_spot_candidate(
    snapshot: dict[str, Any],
    *,
    research_context: list[dict[str, Any]] | None = None,
    event_context: list[dict[str, Any]] | None = None,
    include_news: bool = False,
    trade_proposal: dict[str, Any] | None = None,
    usage_kind: str | None = None,
) -> tuple[dict[str, Any], dict[str, int], str]:
    cfg = load_trading_settings()
    budget = int(cfg.get("trading_token_budget_daily", 0) or 0)
    used = trading_tokens_today()
    if budget > 0 and used >= budget:
        return {
            "action": "hold", "confidence": 0.0, "thesis": "Trading AI token budget reached; Spot live entry withheld.",
            "entry": float(snapshot.get("price", 0.0) or 0.0), "stop_loss": 0.0, "take_profit": 0.0,
            "horizon": "budget_hold", "regime": "", "catalysts": [], "invalidation": "", "risk_notes": ["token_budget"],
            "evidence": [], "used_news": False,
        }, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "budget_guard"

    compact = {k: snapshot.get(k) for k in (
        "symbol", "interval", "price", "signal_price", "bid", "ask", "spread_bps", "atr_pct",
        "return_1_pct", "return_4_pct", "return_12_pct", "return_48_pct",
        "trend_slope_20_pct", "trend_slope_50_pct", "realized_vol_20_pct",
        "volume_ratio_20", "volume_z_20", "range_position_20", "range_position_50",
        "breakout_20_atr", "breakdown_20_atr", "vwap_distance_20_pct",
        "drawdown_from_20_high_pct", "rebound_from_20_low_pct", "body_strength",
        "orderbook_imbalance_10", "ticker_24h_pct", "turnover_24h", "setup_strength",
        "local_bias", "min_order_amt", "base_coin", "quote_coin", "symbol_type", "st_tag",
        "closed_candle_start_ms", "forming_candle_excluded", "evidence_candle_policy", "evidence_model",
    )}
    prompt = (
        "Assess this Bybit Spot opportunity. A deterministic Spot risk/execution layer will independently decide whether any BUY is allowed.\n\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + ("\n\nDETERMINISTIC SPOT TRADE PROPOSAL — review this exact geometry as APPROVE (BUY with same entry/SL/TP) or VETO with HOLD:\n" + json.dumps(trade_proposal or {}, ensure_ascii=False, separators=(",", ":")) if trade_proposal else "")
        + ("\n\nLOCAL TESTED RESEARCH:\n" + json.dumps(research_context or [], ensure_ascii=False, separators=(",", ":")) if research_context else "")
        + ("\n\nCURRENT OFFICIAL EVENT CONTEXT:\n" + json.dumps(event_context or [], ensure_ascii=False, separators=(",", ":")) if event_context else "")
        + ("\n\nUse web search only for current material catalysts that could invalidate or strengthen this candidate." if include_news else "\n\nDo not use web search in this run.")
    )
    tools = [WebSearchTool(search_context_size="low")] if include_news else []
    preferred = "gpt-5.6-sol" if include_news and float(snapshot.get("setup_strength", 0.0) or 0.0) >= 0.82 else "gpt-5.6-terra"
    effort = "medium" if include_news else "low"
    last: Exception | None = None
    for model in fallback_models(preferred):
        try:
            agent = Agent(
                name="Stan Spot Opportunity Analyst",
                model=model,
                instructions=INSTRUCTIONS,
                output_type=SpotAssessment,
                tools=tools,
                model_settings=ModelSettings(reasoning=Reasoning(effort=effort), verbosity="low", max_tokens=1600),
            )
            result = run_sync_resilient(agent, prompt, max_turns=4 if include_news else 3, kind="trading.spot_analysis")
            usage = usage_summary(result)
            record_trading_tokens(int(usage.get("total_tokens", 0) or 0), kind=str(usage_kind or ("spot_news" if include_news else "spot_decision")))
            output = result.final_output
            if not isinstance(output, SpotAssessment):
                raise TypeError("Spot analyst returned unexpected output type")
            return output.model_dump(), usage, model
        except Exception as exc:
            last = exc
            low = str(exc).lower()
            if not ("model" in low and any(x in low for x in ("not found", "access", "available", "permission"))):
                raise
    raise RuntimeError(f"No configured model available for Spot Analyst: {last}")
