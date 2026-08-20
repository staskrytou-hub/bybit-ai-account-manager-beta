from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agents import Agent, ModelSettings, WebSearchTool
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from agent import usage_summary
from model_router import fallback_models
from promotion_store import latest_promotion_scan, store_promotion_scan
from resilience import run_sync_resilient
from trading_config import load_trading_settings
from trading_usage import record_trading_tokens, trading_tokens_today, reserve_ai_call


class PromotionCampaign(BaseModel):
    campaign_key: str = Field(default="", max_length=180)
    name: str = Field(min_length=1, max_length=300)
    source_url: str = Field(default="", max_length=1000)
    source_title: str = Field(default="", max_length=300)
    region: str = Field(default="", max_length=180)
    starts_at: str = Field(default="", max_length=80)
    ends_at: str = Field(default="", max_length=80)
    reward_type: str = Field(default="", max_length=100)
    reward_value_estimate_usd: float | None = Field(default=None, ge=0)
    probabilistic: bool = False
    requires_registration: bool = False
    account_specific: bool = False
    trading_volume_requirement_usd: float | None = Field(default=None, ge=0)
    eligible_symbols: list[str] = Field(default_factory=list, max_length=50)
    tasks: list[str] = Field(default_factory=list, max_length=20)
    restrictions: list[str] = Field(default_factory=list, max_length=20)
    safety_flags: list[str] = Field(default_factory=list, max_length=20)
    actionability: str = Field(default="track", max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)


class PromotionScan(BaseModel):
    scan_summary: str = Field(min_length=1, max_length=2400)
    account_region_notes: list[str] = Field(default_factory=list, max_length=12)
    campaigns: list[PromotionCampaign] = Field(default_factory=list, max_length=30)
    safe_operating_rules: list[str] = Field(default_factory=list, max_length=15)


INSTRUCTIONS = (
    "You are Stan Bybit Promotion Intelligence. Find CURRENT promotions, Rewards Hub tasks, campaigns, airdrops, fee savers, "
    "trial funds, token splashes, trading competitions or other official reward opportunities relevant to the supplied region/account context. "
    "SEARCH ONLY official Bybit properties: bybit.com, bybit.eu, announcements.bybit.com, announcements.bybit.eu and official Bybit Help Center pages. "
    "Do not use affiliate blogs, social-media rumors, or third-party promo pages. Distinguish Bybit Global from Bybit EU and say when eligibility is account-specific or uncertain. "
    "For every campaign capture dates, registration requirement, exact tasks when available, eligible symbols, trading-volume thresholds, reward type/value, restrictions and official source URL. "
    "IMPORTANT SAFETY/COMPLIANCE: never recommend wash trading, matched trading, self-trading, fake volume, multi-account farming, self-referrals, unnecessary leverage, or trades whose primary economic purpose is merely to manufacture volume. "
    "A trading promotion may only be marked actionability='trade_alignment' when it can be pursued by choosing among OTHERWISE VALID strategy/risk-approved opportunities. "
    "Non-trading tasks that require deposits, transfers, card purchases, referrals, registration, spins/draws, staking/Earn subscriptions or account actions should be marked manual_registration/track unless an explicit safe tool exists. "
    "Probabilistic draws/spins must be labeled probabilistic; never treat maximum advertised prize as expected value. "
    "If terms are unclear or personalized Rewards Hub data is inaccessible, set account_specific=true and state the limitation. Return structured data only."
)


def _official_campaigns_only(data: dict[str, Any]) -> dict[str, Any]:
    campaigns = data.get("campaigns") if isinstance(data, dict) else []
    clean: list[dict[str, Any]] = []
    for c in campaigns if isinstance(campaigns, list) else []:
        if not isinstance(c, dict):
            continue
        url = str(c.get("source_url", "")).lower().strip()
        if url and not (
            url.startswith("https://www.bybit.com/") or url.startswith("https://bybit.com/") or
            url.startswith("https://www.bybit.eu/") or url.startswith("https://bybit.eu/") or
            url.startswith("https://announcements.bybit.com/") or url.startswith("https://announcements.bybit.eu/")
        ):
            c["source_url"] = ""
            flags = list(c.get("safety_flags") or [])
            if "unverified_source" not in flags:
                flags.append("unverified_source")
            c["safety_flags"] = flags
            c["confidence"] = min(float(c.get("confidence", 0.0) or 0.0), 0.25)
        clean.append(c)
    data["campaigns"] = clean
    return data


def refresh_promotions(*, region_hint: str = "auto", account_context: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    cfg = load_trading_settings()
    if not bool(cfg.get("promotion_intelligence_enabled", True)):
        return {"disabled": True, "campaigns": []}
    # v4.6.1: automatic live trading must not spend a large web-search model call on
    # promotions. Cached intelligence remains available to Opportunity OS/Browser Operator;
    # the explicit Refresh promotions button (force=True) can still request a fresh scan.
    if not force and not bool(cfg.get("promotion_auto_ai_refresh_enabled", False)):
        cached_auto = latest_promotion_scan()
        if cached_auto:
            data = cached_auto.get("summary") or {"campaigns": cached_auto.get("campaigns", [])}
            if isinstance(data, dict):
                data = dict(data)
                data["auto_ai_refresh_skipped"] = True
                data["scan_summary"] = str(data.get("scan_summary") or "") or "Using cached Promotion Intelligence; automatic paid promotion AI refresh is disabled in v4.6.5."
            return data
        return {"campaigns": [], "scan_summary": "Automatic paid Promotion Intelligence is disabled; use Refresh promotions when a fresh scan is wanted.", "auto_ai_refresh_skipped": True}
    daily_budget = int(cfg.get("trading_token_budget_daily", 0))
    if daily_budget > 0 and trading_tokens_today() >= daily_budget:
        latest_budget = latest_promotion_scan()
        if latest_budget:
            cached = latest_budget.get("summary") or {"campaigns": latest_budget.get("campaigns", [])}
            if isinstance(cached, dict):
                cached = dict(cached); cached["budget_skipped"] = True
            return cached
        return {"budget_skipped": True, "campaigns": [], "scan_summary": "Promotion scan skipped because the daily Trading AI token budget is already reached."}
    latest = latest_promotion_scan()
    if latest and not force:
        try:
            scanned = datetime.fromisoformat(str(latest.get("scanned_at", "")))
            age_hours = (datetime.now(timezone.utc) - scanned).total_seconds() / 3600.0
            if age_hours < int(cfg.get("promotion_refresh_hours", 12)):
                return latest.get("summary") or {"campaigns": latest.get("campaigns", [])}
        except Exception:
            pass

    context = account_context or {}
    safe_context = {
        "region_hint": region_hint or str(cfg.get("promotion_region_hint", "auto")),
        "bybit_key_environment": context.get("environment", cfg.get("bybit_key_environment", "testnet")),
        "account_configured": bool(context.get("configured")),
        "read_only": context.get("read_only"),
        "permissions": context.get("permissions", {}),
        "current_date_utc": datetime.now(timezone.utc).date().isoformat(),
    }
    prompt = (
        "Perform one concise current official Bybit promotion scan for this account context. Prioritize active or imminently expiring campaigns. "
        "Include both trading and non-trading reward opportunities, but label what cannot be automated via the existing API/tooling.\n\n"
        + json.dumps(safe_context, ensure_ascii=False, separators=(",", ":"))
    )
    allowed_ai, reason = reserve_ai_call(
        "promotion_scan",
        budget=daily_budget,
        estimated_tokens=4500,
        max_calls=int(cfg.get("ai_max_calls_daily", 0)),
        kind_budget=int(cfg.get("promotion_ai_tokens_daily", 12000)),
        kind_max_calls=int(cfg.get("promotion_ai_calls_daily", 1)),
        cooldown_key=f"promotions:{safe_context['region_hint']}",
        cooldown_seconds=int(cfg.get("promotion_refresh_hours", 12)) * 3600,
        signature=f"{safe_context['region_hint']}:{safe_context['current_date_utc']}:{safe_context.get('read_only')}",
        ignore_cooldown=False,
    )
    if not allowed_ai:
        latest_guarded = latest_promotion_scan()
        if latest_guarded:
            cached = latest_guarded.get("summary") or {"campaigns": latest_guarded.get("campaigns", [])}
            if isinstance(cached, dict):
                cached = dict(cached); cached["governor_skipped"] = True; cached["governor_reason"] = reason
            return cached
        return {"campaigns": [], "scan_summary": f"Promotion AI scan deferred by token governor: {reason}", "governor_skipped": True}
    preferred = "gpt-5.6-terra"
    last: Exception | None = None
    for model in fallback_models(preferred):
        try:
            agent = Agent(
                name="Stan Bybit Promotion Intelligence",
                model=model,
                instructions=INSTRUCTIONS,
                output_type=PromotionScan,
                tools=[WebSearchTool(search_context_size="low")],
                model_settings=ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low", max_tokens=2600),
            )
            result = run_sync_resilient(agent, prompt, max_turns=4, kind="trading.promotion_scan")
            usage = usage_summary(result)
            record_trading_tokens(int(usage.get("total_tokens", 0)), kind="promotion_scan")
            output = result.final_output
            if not isinstance(output, PromotionScan):
                raise TypeError("Promotion Intelligence returned unexpected output type")
            data = _official_campaigns_only(output.model_dump())
            scan_id = store_promotion_scan(region_hint=safe_context["region_hint"], model=model, usage=usage, summary=data)
            data["scan_id"] = scan_id
            data["model"] = model
            data["usage"] = usage
            return data
        except Exception as exc:
            last = exc
            low = str(exc).lower()
            if not ("model" in low and any(x in low for x in ("not found", "access", "available", "permission"))):
                raise
    raise RuntimeError(f"No configured model available for Promotion Intelligence: {last}")
