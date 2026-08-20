from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

from account_os_store import get_state, record_event, set_state
from bybit_client import BybitClient
from credential_guard import detect_bybit_key_environment
from promotion_store import latest_promotion_scan
from runtime_control import runtime_stop_requested
from spot_engine import assess_and_maybe_execute_spot, monitor_spot_trade, scan_spot_universe
from spot_strategy_research import discover_spot_hypotheses, test_spot_hypotheses
from trading_config import has_bybit_credentials, load_trading_settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _apr(value: Any) -> float:
    text = str(value or "").strip().replace("%", "")
    try:
        return float(text)
    except Exception:
        return 0.0


def _event_bucket(title: str, description: str = "") -> str:
    text = (title + " " + description).lower()
    if "prediction" in text or "predict" in text:
        return "alpha_prediction"
    if "alpha" in text:
        return "alpha"
    if any(x in text for x in ("airdrop", "token splash", "launchpool", "launchpad", "new listing", "listing")):
        return "airdrop_listing"
    if any(x in text for x in ("earn", "apr", "savings", "staking", "stake")):
        return "earn"
    if any(x in text for x in ("futures", "perpetual", "derivatives")):
        return "futures_event"
    if "spot" in text:
        return "spot_event"
    if any(x in text for x in ("competition", "rewards", "reward", "campaign", "giveaway", "spin")):
        return "promotion"
    return "announcement"


def _symbols_from_text(text: str) -> list[str]:
    import re
    found = re.findall(r"\b[A-Z0-9]{2,12}USDT\b", (text or "").upper())
    out: list[str] = []
    for symbol in found:
        if symbol not in out:
            out.append(symbol)
    return out[:12]


def _capabilities() -> dict[str, Any]:
    if not has_bybit_credentials():
        return {
            "configured": False, "futures_trade": False, "spot_trade": False, "earn": False,
            "options": False, "testnet": False, "environment": "none", "read_only": True,
            "unsafe_wallet_permissions": False, "permissions": {},
        }
    try:
        info = detect_bybit_key_environment()
        caps = dict(info.get("capabilities") or {})
        return {
            "configured": True,
            "environment": info.get("environment"),
            "testnet": bool(info.get("testnet")),
            "read_only": bool(info.get("read_only")),
            "unsafe_wallet_permissions": bool(info.get("unsafe_wallet_permissions")),
            "futures_trade": bool(caps.get("futures_trade")),
            "spot_trade": bool(caps.get("spot_trade")),
            "earn": bool(caps.get("earn")),
            "options": bool(caps.get("options")),
            "permissions": info.get("permissions") or {},
            "equity_usdt": info.get("equity_usdt", 0.0),
        }
    except Exception as exc:
        return {"configured": True, "error": f"{type(exc).__name__}: {exc}", "futures_trade": False, "spot_trade": False, "earn": False}


def _announcement_scan(client: BybitClient) -> list[dict[str, Any]]:
    rows = client.get_announcements(locale="en-US", announcement_type="latest_activities", limit=50)
    now_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title", ""))
        description = str(row.get("description", ""))
        end_ms = int(_f(row.get("endDataTimestamp"), 0.0))
        if end_ms and end_ms < now_ms - 12 * 3600 * 1000:
            continue
        out.append({
            "title": title,
            "description": description[:500],
            "bucket": _event_bucket(title, description),
            "symbols": _symbols_from_text(title + " " + description),
            "url": row.get("url", ""),
            "start_ms": int(_f(row.get("startDataTimestamp"), 0.0)),
            "end_ms": end_ms,
            "published_ms": int(_f(row.get("publishTime"), 0.0)),
            "official_api": True,
        })
    return out[:50]


def _earn_scan(client: BybitClient) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for category in ("FlexibleSaving", "OnChain"):
        try:
            for row in client.get_earn_products(category=category):
                if str(row.get("status", "")) != "Available":
                    continue
                products.append({
                    "category": category, "coin": row.get("coin"), "product_id": row.get("productId"),
                    "estimated_apr_pct": _apr(row.get("estimateApr")), "min_stake": _f(row.get("minStakeAmount")),
                    "max_stake": _f(row.get("maxStakeAmount")), "duration": row.get("duration"), "term": row.get("term"),
                    "redeem_processing_minute": row.get("redeemProcessingMinute"), "bonus_events": row.get("bonusEvents") or [],
                    "execution": "discovery_only_v45",
                })
        except Exception:
            continue
    try:
        for row in client.get_fixed_earn_products():
            if str(row.get("status", "")) != "Available":
                continue
            apys = [float(x.get("apy") or 0) for x in list(row.get("tieredApyList") or []) if isinstance(x, dict)]
            products.append({
                "category": "FixedTermSaving", "coin": row.get("coin"), "product_id": row.get("productId"),
                "estimated_apr_pct": max(apys) * (100.0 if apys and max(apys) <= 1.0 else 1.0) if apys else 0.0,
                "min_stake": _f(row.get("minStakeAmount")), "max_stake": _f(row.get("maxStakeAmount")),
                "duration": row.get("duration"), "allow_early_redemption": bool(row.get("allowEarlyRedemption")),
                "execution": "discovery_only_v45",
            })
    except Exception:
        pass
    products.sort(key=lambda x: (float(x.get("estimated_apr_pct", 0.0)), -float(x.get("min_stake", 0.0))), reverse=True)
    return products[:20]


def _research_due(cfg: dict[str, Any]) -> bool:
    raw = get_state("spot_research_last_at", 0)
    try:
        age = time.time() - float(raw or 0)
    except Exception:
        return True
    return age >= float(cfg.get("spot_research_refresh_hours", 8)) * 3600.0


def _relevant_events(events: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    sym = symbol.upper()
    base = sym.removesuffix("USDT")
    out: list[dict[str, Any]] = []
    for row in events:
        title = str(row.get("title", "")).upper()
        syms = [str(x).upper() for x in row.get("symbols") or []]
        if sym in syms or (len(base) >= 3 and base in title):
            out.append(row)
    return out[:8]


def scan_opportunity_os(*, force_research: bool = False, allow_live_spot: bool = True) -> dict[str, Any]:
    if runtime_stop_requested():
        return {"status": "manual_stop", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    cfg = load_trading_settings()
    caps = _capabilities()
    if runtime_stop_requested():
        return {"status": "stopped", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    market_testnet = bool(caps.get("testnet", False))
    public_client = BybitClient(testnet=market_testnet, authenticated=False)

    errors: list[str] = []
    try:
        announcements = _announcement_scan(public_client)
    except Exception as exc:
        announcements = []
        errors.append(f"announcements: {type(exc).__name__}: {exc}")
    try:
        earn = _earn_scan(public_client)
    except Exception as exc:
        earn = []
        errors.append(f"earn: {type(exc).__name__}: {exc}")
    try:
        spot = scan_spot_universe(testnet=market_testnet, top_n=int(cfg.get("spot_universe_top_n", 10)), interval=str(cfg.get("spot_interval", "15")))
    except Exception as exc:
        spot = []
        errors.append(f"spot: {type(exc).__name__}: {exc}")

    if runtime_stop_requested():
        return {"status": "stopped", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    promo = latest_promotion_scan() or {}
    campaign_count = len(promo.get("campaigns") or [])
    permission_gaps: list[dict[str, str]] = []
    if not bool(caps.get("spot_trade")):
        permission_gaps.append({"capability": "Spot live trading", "permission": "SPOT -> Trading (SpotTrade)", "status": "discovery works; live Spot disabled"})
    if not bool(caps.get("earn")):
        permission_gaps.append({"capability": "Earn execution", "permission": "Earn -> Earn", "status": "discovery only in v4.5; do not enable yet"})

    alpha_events = [x for x in announcements if str(x.get("bucket")) in {"alpha", "alpha_prediction", "airdrop_listing"}]
    event_summary = {
        "alpha_prediction": len([x for x in announcements if x.get("bucket") == "alpha_prediction"]),
        "alpha": len([x for x in announcements if x.get("bucket") == "alpha"]),
        "airdrop_listing": len([x for x in announcements if x.get("bucket") == "airdrop_listing"]),
        "spot_events": len([x for x in announcements if x.get("bucket") == "spot_event"]),
        "futures_events": len([x for x in announcements if x.get("bucket") == "futures_event"]),
        "promotions": len([x for x in announcements if x.get("bucket") == "promotion"]),
    }

    research = get_state("spot_research", {})
    if (force_research or _research_due(cfg)) and bool(cfg.get("spot_adaptive_research_enabled", True)) and spot:
        try:
            research_context = {
                "top_spot_market_snapshots": [{k: x.get(k) for k in (
                    "symbol", "setup_strength", "return_4_pct", "return_12_pct", "trend_slope_20_pct", "trend_slope_50_pct",
                    "realized_vol_20_pct", "volume_z_20", "range_position_20", "breakout_20_atr", "vwap_distance_20_pct",
                    "orderbook_imbalance_10", "spread_bps", "turnover_24h", "symbol_type", "st_tag",
                )} for x in spot[:8]],
                "official_current_events": announcements[:18],
                "goal": "Find current Spot hypotheses, not generic indicator recipes. Hypotheses must survive local OOS testing before receiving live support.",
            }
            specs, meta = discover_spot_hypotheses(research_context)
            if bool(meta.get("skipped")) and not specs and isinstance(research, dict) and (research.get("approved") or research.get("tested")):
                research = dict(research)
                research["governor_note"] = str(meta.get("reason", "Spot research deferred by Token Governor"))
                set_state("spot_research", research)
                set_state("spot_research_last_at", time.time())
                record_event("opportunity.spot_research.deferred", research["governor_note"])
                specs = []
            test_client = BybitClient(testnet=market_testnet, authenticated=False)
            fee = 0.001
            if bool(caps.get("configured")):
                try:
                    auth = BybitClient(testnet=market_testnet, authenticated=True)
                    fees = auth.get_fee_rate(category="spot")
                    if fees:
                        fee = abs(_f(fees[0].get("takerFeeRate"), fee))
                except Exception:
                    pass
            if specs:
                tests = test_spot_hypotheses(test_client, [str(x.get("symbol")) for x in spot[:4]], specs, interval=str(cfg.get("spot_interval", "15")), candles=int(cfg.get("spot_backtest_candles", 1400)), fee_rate=fee, slippage_bps=float(cfg.get("spot_research_slippage_bps", 2.0)))
                research = {"meta": meta, **tests, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                set_state("spot_research", research)
                set_state("spot_research_last_at", time.time())
                record_event("opportunity.spot_research", f"Spot adaptive research completed: {len(tests.get('approved') or [])} OOS-supported setup(s)")
        except Exception as exc:
            errors.append(f"spot_research: {type(exc).__name__}: {exc}")

    if runtime_stop_requested():
        return {"status": "stopped", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    approved = list((research or {}).get("approved") or []) if isinstance(research, dict) else []
    spot_result: dict[str, Any] = {}
    if bool(caps.get("configured")):
        try:
            auth_client = BybitClient(testnet=market_testnet, authenticated=True)
            active = monitor_spot_trade(auth_client)
        except Exception as exc:
            active = {"monitor_error": f"{type(exc).__name__}: {exc}"}
    else:
        active = {}

    top = spot[0] if spot else {}
    if top and bool(cfg.get("spot_opportunity_enabled", True)):
        try:
            event_ctx = _relevant_events(announcements, str(top.get("symbol", "")))
            spot_result = assess_and_maybe_execute_spot(
                top, capabilities=caps, research_context=approved, event_context=event_ctx,
                allow_live=allow_live_spot and bool(cfg.get("spot_live_execution_enabled", True)),
            )
            if spot_result.get("execution") in {"submitted", "blocked", "permission_required"}:
                record_event("opportunity.spot_decision", f"Spot {top.get('symbol')}: {spot_result.get('execution')}", {"reason": spot_result.get("reason", ""), "action": spot_result.get("action")})
        except Exception as exc:
            errors.append(f"spot_decision: {type(exc).__name__}: {exc}")

    if runtime_stop_requested():
        return {"status": "stopped", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    state = {
        "version": "4.6.9",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capabilities": caps,
        "permission_gaps": permission_gaps,
        "spot_candidates": spot[:10],
        "spot_research": {"updated_at": (research or {}).get("updated_at", ""), "approved": approved[:10], "tested_count": len((research or {}).get("tested") or [])} if isinstance(research, dict) else {},
        "spot_last_decision": spot_result,
        "spot_active_trade": active,
        "official_events": announcements[:30],
        "event_summary": event_summary,
        "alpha_prediction_items": alpha_events[:12],
        "earn_opportunities": earn[:15],
        "promotion_campaign_count": campaign_count,
        "rules": [
            "Opportunity OS compares multiple sources; it does not force a trade because an event or promotion exists.",
            "No wash trading, self-trading or artificial reward-volume farming.",
            "Spot live execution is unleveraged and permission-gated. Futures hard-risk controls remain independent.",
            "Earn is discovery-only in v4.5 because redemption/liquidity behavior requires a separate capital-allocation governor.",
            "Alpha/Prediction opportunities are discovered and tracked; execution is only allowed when an explicit verified execution module exists.",
        ],
        "errors": errors,
    }
    set_state("opportunity_os", state)
    return state
