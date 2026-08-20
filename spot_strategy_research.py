from __future__ import annotations

import json
import hashlib
from typing import Any, Literal

from agents import Agent, ModelSettings, WebSearchTool
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from adaptive_strategy_lab import ALLOWED_FEATURES, AdaptiveStrategySpec, evaluate_adaptive_robustness, spec_to_dict, validate_spec
from agent import usage_summary
from model_router import fallback_models
from resilience import run_sync_resilient
from trading_config import load_trading_settings
from trading_usage import record_trading_tokens, trading_tokens_today, reserve_ai_call


class SpotRule(BaseModel):
    feature: str = Field(max_length=80)
    op: Literal[">", ">=", "<", "<=", "abs>"]
    value: float


class SpotHypothesis(BaseModel):
    key: str = Field(max_length=80)
    name: str = Field(max_length=120)
    thesis: str = Field(max_length=900)
    long_all: list[SpotRule] = Field(default_factory=list, max_length=6)
    stop_atr: float = Field(ge=0.4, le=4.0)
    target_atr: float = Field(ge=0.6, le=8.0)
    max_hold_bars: int = Field(ge=2, le=96)
    source_context: str = Field(default="", max_length=600)


class SpotDiscovery(BaseModel):
    current_market_themes: list[str] = Field(default_factory=list, max_length=12)
    rejected_ideas: list[str] = Field(default_factory=list, max_length=10)
    hypotheses: list[SpotHypothesis] = Field(default_factory=list, max_length=8)


INSTRUCTIONS = (
    "You are Stan Adaptive Spot Researcher. Research the CURRENT crypto Spot environment and propose falsifiable LONG-only hypotheses. "
    "Do not repeat generic RSI/EMA textbook systems; RSI/EMA are intentionally unavailable as strategy features. "
    "Use current web research as hypothesis inspiration, emphasizing market regime, liquidity, volatility, volume/participation, price structure, "
    "event-driven behavior and reputable research. Social/trader commentary is not proof. "
    "Every hypothesis must compile into the supplied deterministic historical feature whitelist so Stan can backtest and out-of-sample test it locally. "
    "Avoid curve-fit thresholds. Return structured output only and never place orders."
)


def discover_spot_hypotheses(context: dict[str, Any]) -> tuple[list[AdaptiveStrategySpec], dict[str, Any]]:
    cfg = load_trading_settings()
    budget = int(cfg.get("trading_token_budget_daily", 0) or 0)
    used = trading_tokens_today()
    if budget > 0 and used >= int(budget * 0.92):
        return [], {"skipped": True, "reason": f"token_budget {used}/{budget}"}
    compact = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(compact) > 24000:
        compact = compact[:24000] + "..."
    signature = hashlib.sha256(compact.encode("utf-8", errors="ignore")).hexdigest()[:24]
    allowed_ai, reason = reserve_ai_call(
        "spot_strategy_discovery",
        budget=budget,
        estimated_tokens=9000,
        max_calls=int(cfg.get("ai_max_calls_daily", 0)),
        kind_budget=int(cfg.get("spot_strategy_discovery_tokens_daily", 25000)),
        kind_max_calls=int(cfg.get("spot_strategy_discovery_calls_daily", 1)),
        cooldown_key="research:spot_strategy_discovery",
        cooldown_seconds=int(cfg.get("spot_research_refresh_hours", 12)) * 3600,
        signature=signature,
    )
    if not allowed_ai:
        return [], {"skipped": True, "reason": f"AI governor: {reason}"}
    prompt = (
        "Create current-regime Spot hypotheses for local backtesting. Allowed features: "
        + ", ".join(sorted(ALLOWED_FEATURES))
        + ". Operators: >, >=, <, <=, abs>. Only create LONG entry hypotheses; exits are ATR stop/target/time based.\n\n"
        + compact
    )
    last: Exception | None = None
    for model in fallback_models("gpt-5.6-sol"):
        try:
            agent = Agent(
                name="Stan Adaptive Spot Researcher",
                model=model,
                instructions=INSTRUCTIONS,
                output_type=SpotDiscovery,
                tools=[WebSearchTool(search_context_size="low")],
                model_settings=ModelSettings(reasoning=Reasoning(effort="medium"), verbosity="low", max_tokens=3600),
            )
            result = run_sync_resilient(agent, prompt, max_turns=5, kind="trading.spot_strategy_discovery")
            usage = usage_summary(result)
            record_trading_tokens(int(usage.get("total_tokens", 0) or 0), kind="spot_strategy_discovery")
            output = result.final_output
            if not isinstance(output, SpotDiscovery):
                raise TypeError("Spot strategy researcher returned unexpected output type")
            specs: list[AdaptiveStrategySpec] = []
            for i, row in enumerate(output.hypotheses):
                data = row.model_dump()
                data["short_all"] = []
                spec = validate_spec(data, i)
                if spec and spec.long_all:
                    specs.append(spec)
            return specs, {
                "model": model,
                "usage": usage,
                "themes": list(output.current_market_themes),
                "rejected_ideas": list(output.rejected_ideas),
                "hypotheses": [spec_to_dict(x) for x in specs],
            }
        except Exception as exc:
            last = exc
            low = str(exc).lower()
            if not ("model" in low and any(x in low for x in ("not found", "access", "available", "permission"))):
                raise
    raise RuntimeError(f"No configured model available for Spot strategy discovery: {last}")


def test_spot_hypotheses(
    client: Any,
    symbols: list[str],
    specs: list[AdaptiveStrategySpec],
    *,
    interval: str = "15",
    candles: int = 1400,
    fee_rate: float = 0.001,
    slippage_bps: float = 2.0,
) -> dict[str, Any]:
    tested: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []
    for symbol in symbols[:4]:
        try:
            rows = client.get_kline_history(symbol, interval=interval, candles=candles, category="spot")
        except Exception as exc:
            tested.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for spec in specs[:8]:
            result = evaluate_adaptive_robustness(rows, spec, taker_fee_rate=fee_rate, slippage_bps=slippage_bps)
            row = {
                "symbol": symbol,
                "interval": interval,
                "key": spec.key,
                "name": spec.name,
                "thesis": spec.thesis,
                "robust": bool(result.get("robust")),
                "robustness_score": float(result.get("robustness_score", 0.0) or 0.0),
                "train": result.get("train", {}),
                "out_of_sample": result.get("out_of_sample", {}),
            }
            tested.append(row)
            if row["robust"]:
                approved.append(row)
    approved.sort(key=lambda x: float(x.get("robustness_score", 0.0)), reverse=True)
    return {"tested": tested, "approved": approved[:20]}
