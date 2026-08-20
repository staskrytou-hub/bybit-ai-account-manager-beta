from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prelaunch
from account_os_store import get_state as os_get_state
from execution_verifier import confirm_market_entry
from opportunity_manager import build_opportunity_plan
from strategy_governor import build_strategy_governor, strategy_support
from trading_config import apply_safe_autopilot_profile
from trading_store import get_state, set_state


class FilledClient:
    def __init__(self, *args, **kwargs):
        self.stop_calls = []

    def get_order_realtime(self, **kwargs):
        return [{
            "orderId": "oid-1", "orderLinkId": "stan-1", "orderStatus": "Filled",
            "avgPrice": "100", "cumExecQty": "0.1",
        }]

    def get_order_history(self, **kwargs):
        return []

    def get_positions(self, **kwargs):
        return [{"symbol": "TESTUSDT", "size": "0.1", "side": "Buy", "stopLoss": "99", "takeProfit": "102"}]

    def set_trading_stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        return {"retCode": 0}


class AuditClient:
    def __init__(self, *args, **kwargs):
        pass

    def get_wallet_balance(self, coin="USDT"):
        return {"list": [{"totalEquity": "85.25"}]}

    def get_positions(self, **kwargs):
        return []

    def get_open_orders(self, **kwargs):
        return []

    def get_fee_rate(self, **kwargs):
        return [{"symbol": "BTCUSDT", "takerFeeRate": "0.00055"}]

    def get_server_time(self):
        import time
        return {"time": int(time.time() * 1000)}


def main() -> None:
    apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    set_state("execution_safety_lock", "0")

    original_guard = prelaunch.validate_autopilot_key
    original_client = prelaunch.BybitClient
    try:
        prelaunch.validate_autopilot_key = lambda: {
            "ok": True,
            "environment": "mainnet_trade",
            "autopilot_mode": "autopilot_live",
            "live_armed": True,
            "unsafe_wallet_permissions": False,
            "message": "Mainnet futures permissions valid",
            "ips": ["127.0.0.1"],
        }
        prelaunch.BybitClient = AuditClient
        report = prelaunch.run_prelaunch_audit()
        assert report["ready"], report
        assert report["autopilot_mode"] == "autopilot_live"
        assert abs(report["equity_usdt"] - 85.25) < 1e-9
        assert os_get_state("prelaunch_report", {}).get("message") == "READY"
    finally:
        prelaunch.validate_autopilot_key = original_guard
        prelaunch.BybitClient = original_client

    backtests = [{
        "symbol": "BTCUSDT", "interval": "15", "strategy": "trend_breakout", "name": "Trend breakout",
        "strategy_family": "adaptive_current_regime", "adaptive": True,
        "robust": True, "robustness_score": 0.42,
        "out_of_sample": {"expectancy_r": 0.18, "profit_factor": 1.35, "trades": 22},
    }]
    governor = build_strategy_governor(backtests)
    assert governor["approved_count"] == 1, governor
    support = strategy_support("BTCUSDT", "15")
    assert support["supported"] and support["confidence_bump_if_unsupported"] == 0.0, support
    unsupported = strategy_support("ETHUSDT", "15")
    assert not unsupported["supported"] and unsupported["confidence_bump_if_unsupported"] > 0, unsupported

    plan = build_opportunity_plan({"campaigns": [
        {"name": "Eligible futures campaign", "actionability": "trade_alignment", "requires_registration": False, "eligible_symbols": ["BTCUSDT"]},
        {"name": "Register in browser", "actionability": "manual_register", "requires_registration": True},
    ]}, equity_usdt=85.25)
    assert len(plan["automatic_trade_alignment"]) == 1, plan
    assert len(plan["human_action_required"]) == 1, plan

    client = FilledClient()
    result = confirm_market_entry(
        client, symbol="TESTUSDT", order_id="oid-1", order_link_id="stan-1",
        stop_loss=99.0, take_profit=102.0, timeout_seconds=3.0,
    )
    assert result["confirmed"], result
    assert client.stop_calls, "post-fill SL/TP reinforcement was not called"
    assert get_state("execution_safety_lock", "1") == "0"

    print("v4.4 prelaunch/governor/opportunity/execution smoke: PASS")


if __name__ == "__main__":
    main()
