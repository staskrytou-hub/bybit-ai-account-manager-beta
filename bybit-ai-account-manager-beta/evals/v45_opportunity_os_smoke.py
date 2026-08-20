from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-v45-opportunity-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def main() -> None:
    from account_os_store import set_state
    from adaptive_strategy_lab import ALLOWED_FEATURES
    import bybit_client
    import credential_guard
    # Keep this deterministic smoke independent of the OpenAI/Agents SDK runtime.
    import types
    if "agents" not in sys.modules:
        try:
            import agents  # type: ignore  # noqa: F401
        except ModuleNotFoundError:
            fake_agents = types.ModuleType("agents")
            class _RunnerStub:
                @staticmethod
                def run_sync(*args, **kwargs):
                    raise AssertionError("Runner stub should not be called in deterministic v4.5 smoke")
            fake_agents.Runner = _RunnerStub
            sys.modules["agents"] = fake_agents
    fake_spot_ai = types.ModuleType("spot_ai")
    fake_spot_ai.analyze_spot_candidate = lambda *a, **k: ({"action":"hold","confidence":0.0}, {}, "fake")
    sys.modules.setdefault("spot_ai", fake_spot_ai)
    import spot_engine
    from trading_config import apply_safe_autopilot_profile, load_trading_settings

    # v4.5 profile should arm Opportunity OS, while keeping legacy indicator names
    # out of the adaptive hypothesis feature whitelist.
    cfg = apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    loaded = load_trading_settings()
    assert int(loaded.get("autopilot_profile_version", 0)) >= 6, loaded
    assert loaded.get("opportunity_os_enabled") is True, loaded
    assert loaded.get("spot_opportunity_enabled") is True, loaded
    assert not any("ema" in x.lower() or "rsi" in x.lower() for x in ALLOWED_FEATURES), ALLOWED_FEATURES

    # Spot orders must never leak futures-only execution fields.
    captured: dict = {}
    client = bybit_client.BybitClient(testnet=False, authenticated=False)
    old_request = client._request
    try:
        def fake_request(method, path, *, params=None, body=None, private=False):
            captured.update({"method": method, "path": path, "body": dict(body or {}), "private": private})
            return {"retCode": 0, "result": {"orderId": "spot-eval-order"}}
        client._request = fake_request  # type: ignore[method-assign]
        client.place_spot_order(
            symbol="TESTUSDT", side="Buy", qty="5", order_type="Limit", price="1.001",
            take_profit="1.2", stop_loss="0.9", order_link_id="stan-v45-eval",
        )
    finally:
        client._request = old_request  # type: ignore[method-assign]
    body = captured["body"]
    assert captured["path"] == "/v5/order/create", captured
    assert body["category"] == "spot", body
    assert body["isLeverage"] == 0, body
    assert "positionIdx" not in body and "reduceOnly" not in body, body
    assert body.get("takeProfit") == "1.2" and body.get("stopLoss") == "0.9", body

    # Permission parsing: SpotTrade enables Spot without being treated as a dangerous Wallet permission.
    class GuardClient:
        def __init__(self, *a, **k): pass
        def get_api_key_info(self):
            return {
                "readOnly": 0,
                "permissions": {
                    "ContractTrade": ["Order", "Position"],
                    "Spot": ["SpotTrade"],
                    "Earn": [],
                    "Options": [],
                    "Wallet": [],
                },
            }
        def get_wallet_balance(self, coin=""):
            return {"list": [{"totalEquity": "86", "coin": [{"coin": "USDT", "walletBalance": "86"}]}]}
        def get_positions(self, **kwargs): return []
        def get_server_time(self): return {"time": 0}
        def get_fee_rate(self, **kwargs): return []
    old_guard_client = credential_guard.BybitClient
    try:
        credential_guard.BybitClient = GuardClient  # type: ignore[assignment]
        info = credential_guard._inspect(False, "x", "y")
    finally:
        credential_guard.BybitClient = old_guard_client  # type: ignore[assignment]
    assert info["capabilities"]["futures_trade"] is True, info
    assert info["capabilities"]["spot_trade"] is True, info
    assert info["unsafe_wallet_permissions"] is False, info

    # A calculated Spot position below the exchange minimum may be lifted to the
    # minimum only if its actual stop-risk still fits the absolute account envelope.
    class SpotClient:
        last_instance = None
        def __init__(self, *a, **k):
            SpotClient.last_instance = self
            self.orders = []
        def get_unified_wallet(self, coin=""):
            if coin and coin.upper() != "USDT":
                return {"list": [{"totalEquity": "86", "coin": [{"coin": coin.upper(), "walletBalance": "0"}]}]}
            return {"list": [{"totalEquity": "86", "totalWalletBalance": "86", "coin": [{"coin": "USDT", "walletBalance": "86"}]}]}
        def place_spot_order(self, **kwargs):
            self.orders.append(dict(kwargs))
            return {"retCode": 0, "result": {"orderId": "safe-min-order"}}

    old_spot_client = spot_engine.BybitClient
    old_analyze = spot_engine.analyze_spot_candidate
    old_reserve = spot_engine.reserve_ai_call
    try:
        spot_engine.BybitClient = SpotClient  # type: ignore[assignment]
        spot_engine.reserve_ai_call = lambda *a, **k: (True, "eval")  # type: ignore[assignment]
        spot_engine.analyze_spot_candidate = lambda *a, **k: (
            {"action": "buy", "confidence": 0.99, "entry": 1.0, "stop_loss": 0.90, "take_profit": 1.20, "thesis": "eval"},
            {}, "eval-model"
        )
        set_state("spot_active_trade", {})
        safe_snapshot = {
            "symbol": "TESTUSDT", "category": "spot", "interval": "15", "price": 1.0, "signal_price": 1.0, "ask": 1.0,
            "spread_bps": 1.0, "setup_strength": 0.90, "local_bias": "buy_candidate",
            "atr14": 0.20, "atr_pct": 20.0, "vwap_distance_20_pct": 0.10, "range_position_20": 0.60,
            "return_4_pct": 1.0, "return_12_pct": 2.0, "volume_ratio_20": 1.5, "orderbook_imbalance_10": 0.2,
            "closed_candle_start_ms": 1234567890000, "min_order_amt": 5.0,
            "tick_size": "0.001", "base_precision": "1", "base_coin": "TEST", "st_tag": "0",
        }
        safe = spot_engine.assess_and_maybe_execute_spot(
            safe_snapshot, capabilities={"spot_trade": True, "testnet": False}, research_context=[], event_context=[], allow_live=True
        )
        assert safe.get("execution") == "submitted", safe
        assert safe.get("trade", {}).get("min_order_override") is True, safe
        assert float(safe.get("trade", {}).get("actual_risk_pct", 99)) <= float(cfg.get("spot_absolute_risk_cap_pct", 1.50)) + 1e-9, safe

        # Unsafe exchange minimum remains blocked, not forced through.
        set_state("spot_active_trade", {})
        unsafe_snapshot = dict(safe_snapshot)
        unsafe_snapshot["min_order_amt"] = 20.0
        unsafe_snapshot["closed_candle_start_ms"] = int(safe_snapshot["closed_candle_start_ms"]) + 900000
        unsafe = spot_engine.assess_and_maybe_execute_spot(
            unsafe_snapshot, capabilities={"spot_trade": True, "testnet": False}, research_context=[], event_context=[], allow_live=True
        )
        assert unsafe.get("execution") == "blocked", unsafe
        assert "minimum" in str(unsafe.get("reason", "")).lower(), unsafe
    finally:
        spot_engine.BybitClient = old_spot_client  # type: ignore[assignment]
        spot_engine.analyze_spot_candidate = old_analyze  # type: ignore[assignment]
        spot_engine.reserve_ai_call = old_reserve  # type: ignore[assignment]

    print("v4.5 Opportunity OS / Spot permission / executable-minimum smoke: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
