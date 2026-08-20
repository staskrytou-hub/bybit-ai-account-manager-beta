from __future__ import annotations

import json
from typing import Any

from agents import Agent, ModelSettings, WebSearchTool
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from agent import usage_summary
from model_router import choose_model, fallback_models
from resilience import run_sync_resilient
from usage import record_usage


class ProfessionalResearchSummary(BaseModel):
    market_regime: str = Field(min_length=1, max_length=2200)
    priority_symbols: list[str] = Field(default_factory=list, max_length=8)
    strategy_findings: list[str] = Field(default_factory=list, max_length=12)
    current_catalysts: list[str] = Field(default_factory=list, max_length=12)
    major_risks: list[str] = Field(default_factory=list, max_length=12)
    operating_rules: list[str] = Field(default_factory=list, max_length=12)
    next_research_tasks: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)


INSTRUCTIONS = (
    "You are Stan Chief Futures Research Analyst. You receive deterministic Bybit market/account diagnostics and local backtest results. "
    "Synthesize them into a professional research baseline; do not place orders and do not promise profit. "
    "Use Web Search to identify CURRENT high-impact macro, crypto-market, regulatory, exchange or liquidity catalysts that can materially affect futures, and to identify reputable research-worthy futures strategy families or market microstructure concepts. Avoid social-media trade calls and influencer predictions. "
    "Separate measured exchange/backtest evidence from web context. Treat small-sample backtests as hypotheses, not validated strategies. "
    "Compare external research concepts against the supplied local strategy tests. Prioritize liquid markets, robustness, risk control, execution quality, and research steps that can be verified statistically. "
    "If promotion intelligence is supplied, treat it as an economic overlay only: identify legitimate reward opportunities but never manufacture trading volume, loosen risk, chase a draw, or convert a weak setup into a trade. Promotions may only align with otherwise valid risk-approved trades. "
    "Return the structured schema only."
)


def synthesize_professional_research(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int], str]:
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(compact) > 30000:
        compact = compact[:30000] + "..."
    prompt = (
        "Build the initial professional futures research baseline for Stan Trading Core. "
        "Use current web research for only material market-wide catalysts; do not search for social-media trade calls.\n\n"
        + compact
    )
    preferred = "gpt-5.6-sol"
    last: Exception | None = None
    for model in fallback_models(preferred):
        try:
            agent = Agent(
                name="Stan Chief Futures Research Analyst",
                model=model,
                instructions=INSTRUCTIONS,
                output_type=ProfessionalResearchSummary,
                tools=[WebSearchTool(search_context_size="low")],
                model_settings=ModelSettings(reasoning=Reasoning(effort="medium"), verbosity="low", max_tokens=4000),
            )
            result = run_sync_resilient(agent, prompt, max_turns=5, kind="trading.bootstrap_research")
            usage = usage_summary(result)
            record_usage(usage)
            output = result.final_output
            if not isinstance(output, ProfessionalResearchSummary):
                raise TypeError("Chief research analyst returned unexpected output type")
            return output.model_dump(), usage, model
        except Exception as exc:
            last = exc
            low = str(exc).lower()
            if not ("model" in low and any(x in low for x in ("not found", "access", "available", "permission"))):
                raise
    raise RuntimeError(f"No configured model available for Chief Futures Research Analyst: {last}")
