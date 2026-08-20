from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from bybit_client import BybitClient
from market_analysis import build_market_snapshot
from artifacts import register_artifact
from paths import WORKSPACE_DIR
from research_store import create_bootstrap, finish_bootstrap, store_strategy_result
from promotion_ai import refresh_promotions
from strategy_lab import STRATEGIES, evaluate_strategy_robustness
from adaptive_strategy_lab import evaluate_adaptive_robustness, spec_to_dict
from strategy_discovery_ai import discover_strategy_hypotheses
from strategy_governor import build_strategy_governor
from opportunity_manager import build_opportunity_plan
from trading_config import has_bybit_credentials, load_trading_settings
from trading_research_ai import synthesize_professional_research
from trading_usage import record_trading_tokens, reserve_ai_call
from runtime_control import RuntimeStoppedError, runtime_stop_requested
from universe_scanner import multi_timeframe_regime, scan_linear_universe

Progress = Callable[[dict[str, Any]], None]


def _ensure_runtime_active() -> None:
    if runtime_stop_requested():
        raise RuntimeStoppedError("Stan stopped during professional bootstrap")


def _emit(cb: Progress | None, stage: str, message: str, **extra: Any) -> None:
    if cb:
        cb({"stage": stage, "message": message, **extra})


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _account_diagnostics(cfg: dict[str, Any]) -> dict[str, Any]:
    if not has_bybit_credentials():
        return {"configured": False, "note": "No Bybit private API credentials configured; public-market research still works."}
    env = str(cfg.get("bybit_key_environment", "testnet"))
    testnet = env == "testnet"
    client = BybitClient(testnet=testnet, authenticated=True)
    key_info = client.get_api_key_info()
    wallet = client.get_wallet_balance("USDT")
    positions = client.get_positions(settle_coin="USDT")
    executions = client.get_executions(limit=100)
    try:
        closed_pnl = client.get_closed_pnl(limit=100)
    except Exception:
        closed_pnl = []
    try:
        fees = client.get_fee_rate()
    except Exception:
        fees = []
    active_positions = [p for p in positions if _float(p.get("size")) > 0]
    pnl_values = [_float(x.get("closedPnl")) for x in closed_pnl]
    pnl_wins = sum(1 for x in pnl_values if x > 0)
    pnl_losses = sum(1 for x in pnl_values if x < 0)
    account_trade_stats = {
        "closed_positions_sample": len(pnl_values),
        "sample_net_closed_pnl": round(sum(pnl_values), 8),
        "sample_win_rate": round(pnl_wins / len(pnl_values), 4) if pnl_values else None,
        "wins": pnl_wins,
        "losses": pnl_losses,
    }
    permissions = key_info.get("permissions") or {}
    return {
        "configured": True,
        "environment": env,
        "read_only": int(key_info.get("readOnly", -1) or 0) == 1,
        "permissions": permissions,
        "wallet": wallet,
        "open_positions": active_positions,
        "recent_execution_count": len(executions),
        "recent_executions": executions[:20],
        "account_trade_stats": account_trade_stats,
        "recent_closed_pnl": closed_pnl[:20],
        "fee_rates": fees[:20],
        "security_notes": [
            "Mainnet research should use a dedicated read-only key until live execution is explicitly enabled in a later release.",
            "Withdrawal/transfer permissions are not required for Stan Trading Core.",
        ],
    }


def _effective_fee(account: dict[str, Any]) -> float:
    for item in account.get("fee_rates", []) if isinstance(account, dict) else []:
        value = _float(item.get("takerFeeRate"), 0.0)
        if value > 0:
            return value
    return 0.00055




def _write_research_artifacts(report: dict[str, Any]) -> list[str]:
    folder = WORKSPACE_DIR / "TradingResearch"
    folder.mkdir(parents=True, exist_ok=True)
    account = report.get("account") or {}
    safe = {
        "run_id": report.get("run_id"),
        "completed_at": report.get("completed_at"),
        "account_summary": {
            "configured": account.get("configured"),
            "environment": account.get("environment"),
            "read_only": account.get("read_only"),
            "permissions": account.get("permissions"),
            "open_position_count": len(account.get("open_positions", [])) if isinstance(account.get("open_positions"), list) else 0,
            "recent_execution_count": account.get("recent_execution_count", 0),
            "account_trade_stats": account.get("account_trade_stats", {}),
        },
        "universe": report.get("universe", []),
        "regimes": report.get("regimes", []),
        "derivatives_snapshots": report.get("derivatives_snapshots", []),
        "backtests": report.get("backtests", []),
        "adaptive_strategy_discovery": report.get("adaptive_strategy_discovery", {}),
        "adaptive_strategy_specs": report.get("adaptive_strategy_specs", []),
        "promotions": report.get("promotions", {}),
        "chief_research": report.get("chief_research", {}),
        "chief_model": report.get("chief_model", ""),
        "chief_usage": report.get("chief_usage", {}),
    }
    json_path = folder / "professional_baseline_latest.json"
    json_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    chief = safe.get("chief_research") or {}
    lines = [
        "# Stan Professional Futures Research Baseline",
        "",
        f"Completed: {safe.get('completed_at')}",
        f"Chief model: {safe.get('chief_model')}",
        "",
        "## Market regime",
        str(chief.get("market_regime", "")),
        "",
        "## Priority symbols",
        "\n".join(f"- {x}" for x in chief.get("priority_symbols", [])),
        "",
        "## Promotion intelligence",
        str((safe.get("promotions") or {}).get("scan_summary", "")),
        "",
        "## Major risks",
        "\n".join(f"- {x}" for x in chief.get("major_risks", [])),
        "",
        "## Operating rules",
        "\n".join(f"- {x}" for x in chief.get("operating_rules", [])),
        "",
        "## Next research tasks",
        "\n".join(f"- {x}" for x in chief.get("next_research_tasks", [])),
    ]
    md_path = folder / "professional_baseline_latest.md"
    md_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    rels=[]
    for path in (json_path, md_path):
        rel = path.relative_to(WORKSPACE_DIR).as_posix()
        register_artifact(rel, kind="file", source="trading_research", description="Stan professional futures research baseline")
        rels.append(rel)
    return rels


def run_professional_bootstrap(progress: Progress | None = None) -> dict[str, Any]:
    _ensure_runtime_active()
    cfg = load_trading_settings()
    env = str(cfg.get("bybit_key_environment", "testnet"))
    run_id = create_bootstrap(env)
    report: dict[str, Any] = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        _ensure_runtime_active()
        _emit(progress, "account", "Validating Bybit connection and API permissions...")
        account = _account_diagnostics(cfg)
        report["account"] = account

        _ensure_runtime_active()
        promotions: dict[str, Any] = {"campaigns": []}
        if bool(cfg.get("promotion_intelligence_enabled", True)):
            _emit(progress, "promotions", "Scanning official Bybit / Bybit EU promotions, Rewards Hub campaigns and eligibility rules...")
            try:
                promotions = refresh_promotions(
                    region_hint=str(cfg.get("promotion_region_hint", "auto")),
                    account_context=account,
                    force=False,
                )
            except Exception as promo_exc:
                promotions = {"campaigns": [], "error": f"{type(promo_exc).__name__}: {promo_exc}"}
        report["promotions"] = promotions
        equity_hint = 0.0
        try:
            wallet_items = list((account.get("wallet") or {}).get("list") or [])
            if wallet_items:
                equity_hint = float(wallet_items[0].get("totalEquity") or wallet_items[0].get("totalWalletBalance") or 0.0)
        except Exception:
            equity_hint = 0.0
        report["opportunity_plan"] = build_opportunity_plan(promotions, equity_usdt=equity_hint)

        _ensure_runtime_active()
        _emit(progress, "universe", "Scanning the most liquid Bybit USDT perpetual markets...")
        top_n = int(cfg.get("research_universe_top_n", 12))
        universe = scan_linear_universe(top_n=top_n, testnet=False)
        report["universe"] = universe

        research_symbols = [str(x["symbol"]) for x in universe[: int(cfg.get("research_regime_symbols", 6))]]
        _emit(progress, "regime", f"Building multi-timeframe regimes for {len(research_symbols)} liquid markets...")
        regimes: list[dict[str, Any]] = []
        for idx, symbol in enumerate(research_symbols, start=1):
            _ensure_runtime_active()
            _emit(progress, "regime", f"Multi-timeframe analysis {idx}/{len(research_symbols)}: {symbol}")
            try:
                regimes.append(multi_timeframe_regime(symbol, ["5", "15", "60", "240"], testnet=False))
            except Exception as exc:
                regimes.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
        report["regimes"] = regimes

        derivative_symbols = research_symbols[: min(3, len(research_symbols))]
        _emit(progress, "derivatives", f"Reading funding/OI/orderbook/long-short context for {len(derivative_symbols)} priority markets...")
        derivatives: list[dict[str, Any]] = []
        for symbol in derivative_symbols:
            _ensure_runtime_active()
            try:
                derivatives.append(build_market_snapshot(symbol, "15", testnet_market_data=False))
            except Exception as exc:
                derivatives.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
        report["derivatives_snapshots"] = derivatives

        adaptive_specs = []
        adaptive_discovery: dict[str, Any] = {"hypotheses": [], "skipped": True}
        if bool(cfg.get("adaptive_strategy_discovery_enabled", True)):
            _ensure_runtime_active()
            _emit(progress, "strategy_discovery", "Researching current market structure and proposing falsifiable adaptive strategy hypotheses...")
            discovery_context = {
                "top_liquid_markets": universe[:10],
                "multi_timeframe_regimes": regimes,
                "derivatives_snapshots": derivatives,
                "account_trade_stats": account.get("account_trade_stats", {}),
                "promotion_context": {
                    "campaign_count": len(promotions.get("campaigns") or []),
                    "rule": "promotions are secondary overlays only and never define a trade thesis",
                },
                "instruction": (
                    "Study what is currently relevant in futures market structure and derivatives positioning, then propose testable rules. "
                    "Do not anchor to RSI/EMA textbook setups. Every idea must be falsifiable with local historical features before live use."
                ),
            }
            try:
                adaptive_specs, adaptive_discovery = discover_strategy_hypotheses(discovery_context)
            except Exception as discovery_exc:
                adaptive_discovery = {
                    "hypotheses": [],
                    "error": f"{type(discovery_exc).__name__}: {discovery_exc}",
                    "note": "Adaptive discovery failed; benchmark research continues but does not become a mandatory live strategy gate.",
                }
        report["adaptive_strategy_discovery"] = adaptive_discovery
        report["adaptive_strategy_specs"] = [spec_to_dict(x) for x in adaptive_specs]

        backtest_symbols = research_symbols[: int(cfg.get("research_backtest_symbols", 3))]
        fee = _effective_fee(account)
        slippage = float(cfg.get("research_slippage_bps", 1.5))
        candles = int(cfg.get("research_backtest_candles", 1600))
        backtests: list[dict[str, Any]] = []
        market_client = BybitClient(testnet=False)
        for sidx, symbol in enumerate(backtest_symbols, start=1):
            _ensure_runtime_active()
            for interval in ("15", "60"):
                _ensure_runtime_active()
                _emit(progress, "backtest", f"Backtesting {symbol} {interval}m ({sidx}/{len(backtest_symbols)})...")
                try:
                    rows = market_client.get_kline_history(symbol, interval=interval, candles=candles)
                    # Legacy textbook systems remain BENCHMARKS only. They do not define Stan's live strategy.
                    for spec in STRATEGIES:
                        _ensure_runtime_active()
                        result = evaluate_strategy_robustness(rows, spec, taker_fee_rate=fee, slippage_bps=slippage)
                        item = {
                            "symbol": symbol, "interval": interval, "strategy": spec.key, "name": spec.name,
                            "strategy_family": "benchmark_legacy", "adaptive": False, **result,
                        }
                        backtests.append(item)
                        store_strategy_result(run_id, symbol, interval, {
                            "strategy": spec.key, "name": spec.name, "strategy_family": "benchmark_legacy",
                            "robust": result.get("robust"), "robustness_score": result.get("robustness_score"),
                            "full": result.get("full", {}), "out_of_sample": result.get("out_of_sample", {}),
                        })

                    # Current-regime adaptive hypotheses are the research candidates that may support live decisions.
                    for spec in adaptive_specs[: int(cfg.get("adaptive_strategy_hypotheses", 8))]:
                        _ensure_runtime_active()
                        result = evaluate_adaptive_robustness(rows, spec, taker_fee_rate=fee, slippage_bps=slippage)
                        item = {
                            "symbol": symbol, "interval": interval, "strategy": spec.key, "name": spec.name,
                            "strategy_family": "adaptive_current_regime", "adaptive": True,
                            "thesis": spec.thesis, **result,
                        }
                        backtests.append(item)
                        store_strategy_result(run_id, symbol, interval, {
                            "strategy": spec.key, "name": spec.name, "strategy_family": "adaptive_current_regime",
                            "adaptive": True, "thesis": spec.thesis,
                            "robust": result.get("robust"), "robustness_score": result.get("robustness_score"),
                            "full": result.get("full", {}), "out_of_sample": result.get("out_of_sample", {}),
                        })
                except Exception as exc:
                    backtests.append({"symbol": symbol, "interval": interval, "error": f"{type(exc).__name__}: {exc}"})
        backtests.sort(key=lambda x: float(x.get("robustness_score", 0)), reverse=True)
        report["backtests"] = backtests
        report["strategy_governor"] = build_strategy_governor(backtests)

        _ensure_runtime_active()
        _emit(progress, "chief_ai", "Chief Futures Research Analyst is synthesizing exchange evidence + current macro/news...")
        ai_payload = {
            "account": {
                "configured": account.get("configured"),
                "environment": account.get("environment"),
                "read_only": account.get("read_only"),
                "permissions": account.get("permissions"),
                "open_position_count": len(account.get("open_positions", [])) if isinstance(account.get("open_positions"), list) else 0,
                "recent_execution_count": account.get("recent_execution_count", 0),
                "account_trade_stats": account.get("account_trade_stats", {}),
            },
            "top_liquid_markets": universe[:10],
            "multi_timeframe_regimes": regimes,
            "derivatives_snapshots": derivatives,
            "strategy_tests": [
                {
                    "symbol": x.get("symbol"), "interval": x.get("interval"), "name": x.get("name"),
                    "robust": x.get("robust"), "robustness_score": x.get("robustness_score"),
                    "full": x.get("full"), "out_of_sample": x.get("out_of_sample"),
                }
                for x in backtests[:18]
            ],
            "strategy_governor": report.get("strategy_governor", {}),
            "adaptive_strategy_discovery": {
                "themes": adaptive_discovery.get("current_market_themes", []),
                "hypotheses": [
                    {"key": x.key, "name": x.name, "thesis": x.thesis, "source_context": x.source_context}
                    for x in adaptive_specs[:8]
                ],
                "rule": "Adaptive hypotheses are research candidates only until local out-of-sample tests support them.",
            },
            "promotion_intelligence": {
                "scan_summary": promotions.get("scan_summary", ""),
                "account_region_notes": promotions.get("account_region_notes", []),
                "campaigns": [
                    {
                        "name": c.get("name"), "region": c.get("region"), "ends_at": c.get("ends_at"),
                        "reward_type": c.get("reward_type"), "probabilistic": c.get("probabilistic"),
                        "requires_registration": c.get("requires_registration"),
                        "trading_volume_requirement_usd": c.get("trading_volume_requirement_usd"),
                        "eligible_symbols": c.get("eligible_symbols", []), "actionability": c.get("actionability"),
                        "restrictions": c.get("restrictions", []), "safety_flags": c.get("safety_flags", []),
                    }
                    for c in (promotions.get("campaigns") or [])[:15] if isinstance(c, dict)
                ],
                "hard_rule": "Never manufacture volume or loosen risk for a promotion. Only align promotions with independently valid trades.",
            },
            "risk_policy": {
                "risk_per_trade_pct": cfg.get("risk_per_trade_pct"),
                "max_daily_loss_pct": cfg.get("max_daily_loss_pct"),
                "max_leverage": cfg.get("max_leverage"),
                "max_positions": cfg.get("max_positions"),
                "min_confidence": cfg.get("min_confidence"),
                "executable_min_order_override": cfg.get("executable_min_order_override"),
                "min_order_override_max_risk_pct": cfg.get("min_order_override_max_risk_pct"),
            },
        }
        allowed_chief, chief_reason = reserve_ai_call(
            "research_chief",
            budget=int(cfg.get("trading_token_budget_daily", 0)),
            estimated_tokens=11000,
            max_calls=int(cfg.get("ai_max_calls_daily", 0)),
            kind_budget=int(cfg.get("research_chief_tokens_daily", 30000)),
            kind_max_calls=int(cfg.get("research_chief_calls_daily", 1)),
            cooldown_key="research:chief",
            cooldown_seconds=int(cfg.get("research_refresh_hours", 12)) * 3600,
            signature=f"{datetime.now(timezone.utc).date().isoformat()}:{','.join(research_symbols[:4])}:{len(backtests)}",
        )
        if allowed_chief:
            _ensure_runtime_active()
            chief, usage, model = synthesize_professional_research(ai_payload)
            record_trading_tokens(int(usage.get("total_tokens", 0)), kind="research_chief")
        else:
            approved_rows = list((report.get("strategy_governor") or {}).get("approved") or [])
            chief = {
                "market_regime": f"Local professional research completed; web/LLM synthesis deferred by Token Governor: {chief_reason}",
                "priority_symbols": research_symbols[:6],
                "strategy_findings": [f"{x.get('symbol')} {x.get('interval')}m {x.get('name')}" for x in approved_rows[:8]],
                "current_catalysts": [],
                "major_risks": ["AI macro/news synthesis deferred; deterministic exchange monitoring remains active."],
                "operating_rules": ["Do not trade solely because AI synthesis is unavailable; local risk and execution gates remain authoritative."],
                "next_research_tasks": ["Run Chief synthesis when token budget is available."],
                "confidence": 0.0,
            }
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            model = "token_governor_local_fallback"
        _ensure_runtime_active()
        report["chief_research"] = chief
        report["chief_model"] = model
        report["chief_usage"] = usage
        report["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report["artifacts"] = _write_research_artifacts(report)
        summary = str(chief.get("market_regime", "Professional bootstrap completed."))
        finish_bootstrap(run_id, status="completed", report=report, summary=summary)
        _emit(progress, "complete", "Professional first-run research completed.", run_id=run_id)
        return report
    except RuntimeStoppedError as exc:
        report["cancelled"] = True
        report["error"] = str(exc)
        finish_bootstrap(run_id, status="cancelled", report=report, error=report["error"])
        _emit(progress, "cancelled", report["error"], run_id=run_id)
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        finish_bootstrap(run_id, status="failed", report=report, error=report["error"])
        _emit(progress, "failed", report["error"], run_id=run_id)
        raise
