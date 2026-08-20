from __future__ import annotations

import json
import hashlib
from typing import Any, Literal

from agents import Agent, ModelSettings, WebSearchTool
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from adaptive_strategy_lab import ALLOWED_FEATURES, AdaptiveStrategySpec, load_adaptive_specs, store_adaptive_specs, validate_spec
from agent import usage_summary
from model_router import fallback_models
from resilience import run_sync_resilient
from trading_usage import record_trading_tokens, trading_tokens_today, reserve_ai_call
from trading_config import load_trading_settings


class DiscoveryRule(BaseModel):
    feature: str = Field(max_length=80)
    op: Literal[">", ">=", "<", "<=", "abs>"]
    value: float


class StrategyHypothesis(BaseModel):
    key: str = Field(max_length=80)
    name: str = Field(max_length=120)
    thesis: str = Field(max_length=900)
    long_all: list[DiscoveryRule] = Field(default_factory=list, max_length=6)
    short_all: list[DiscoveryRule] = Field(default_factory=list, max_length=6)
    stop_atr: float = Field(ge=0.4, le=4.0)
    target_atr: float = Field(ge=0.6, le=8.0)
    max_hold_bars: int = Field(ge=2, le=96)
    source_context: str = Field(default="", max_length=600)


class StrategyDiscovery(BaseModel):
    current_market_themes: list[str] = Field(default_factory=list, max_length=12)
    rejected_ideas: list[str] = Field(default_factory=list, max_length=10)
    hypotheses: list[StrategyHypothesis] = Field(default_factory=list, max_length=10)


INSTRUCTIONS = (
    "You are Stan Adaptive Futures Researcher. Your job is NOT to repeat generic RSI/EMA textbook systems. "
    "Use current web research plus supplied Bybit market evidence to propose a small number of falsifiable strategy hypotheses suited to the CURRENT market regime. "
    "Search broadly for current market structure, volatility regimes, derivatives positioning, liquidity conditions, event risk and reputable research concepts. "
    "Treat social-media or trader commentary only as hypothesis inspiration, never as fact. Prefer exchange/official data and reputable primary research when available. "
    "Every hypothesis MUST compile into the provided whitelist of deterministic historical features and operators, so Stan can backtest it locally before it influences live trading. "
    "No indicator is mandatory. RSI/EMA are intentionally not in the allowed feature list. Avoid curve-fit thresholds and avoid more than 4 rules per side unless essential. "
    "Return structured data only. Do not place trades."
)


def discover_strategy_hypotheses(context: dict[str, Any]) -> tuple[list[AdaptiveStrategySpec], dict[str, Any]]:
    cfg = load_trading_settings()
    budget = int(cfg.get("trading_token_budget_daily", 0) or 0)
    used = trading_tokens_today()
    if budget > 0 and used >= int(budget * 0.90):
        existing = load_adaptive_specs()
        return existing, {
            "skipped": True,
            "reason": f"Trading AI token budget protection: {used:,}/{budget:,} tokens used today",
            "retained_hypotheses": len(existing),
        }
    allowed = sorted(ALLOWED_FEATURES)
    compact = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(compact) > 26000:
        compact = compact[:26000] + "..."
    signature = hashlib.sha256(compact.encode("utf-8", errors="ignore")).hexdigest()[:24]
    allowed_ai, reason = reserve_ai_call(
        "strategy_discovery",
        budget=budget,
        estimated_tokens=10000,
        max_calls=int(cfg.get("ai_max_calls_daily", 0)),
        kind_budget=int(cfg.get("strategy_discovery_tokens_daily", 25000)),
        kind_max_calls=int(cfg.get("strategy_discovery_calls_daily", 1)),
        cooldown_key="research:futures_strategy_discovery",
        cooldown_seconds=int(cfg.get("research_refresh_hours", 12)) * 3600,
        signature=signature,
    )
    if not allowed_ai:
        existing = load_adaptive_specs()
        return existing, {"skipped": True, "reason": f"AI governor: {reason}", "retained_hypotheses": len(existing)}
    prompt = (
        "Create current-regime futures strategy hypotheses for local falsification/backtesting. "
        "Allowed historical features: " + ", ".join(allowed) + ". "
        "Operators: >, >=, <, <=, abs>. Use thresholds with realistic scale. Do not use features outside the whitelist.\n\n" + compact
    )
    last: Exception | None = None
    for model in fallback_models("gpt-5.6-sol"):
        try:
            agent = Agent(
                name="Stan Adaptive Strategy Researcher", model=model, instructions=INSTRUCTIONS,
                output_type=StrategyDiscovery, tools=[WebSearchTool(search_context_size="low")],
                model_settings=ModelSettings(reasoning=Reasoning(effort="medium"), verbosity="low", max_tokens=4000),
            )
            result = run_sync_resilient(agent, prompt, max_turns=5, kind="trading.strategy_discovery")
            usage = usage_summary(result)
            record_trading_tokens(int(usage.get("total_tokens", 0) or 0), kind="strategy_discovery")
            output = result.final_output
            if not isinstance(output, StrategyDiscovery):
                raise TypeError("Strategy discovery returned unexpected output")
            raw = output.model_dump()
            specs: list[AdaptiveStrategySpec] = []
            for i, row in enumerate(raw.get("hypotheses") or []):
                spec = validate_spec(row, i)
                if spec:
                    specs.append(spec)
            store_adaptive_specs(specs)
            return specs, {"model": model, "usage": usage, **raw}
        except Exception as exc:
            last = exc
            low = str(exc).lower()
            if not ("model" in low and any(x in low for x in ("not found", "access", "available", "permission"))):
                raise
    raise RuntimeError(f"No configured model available for adaptive strategy discovery: {last}")
