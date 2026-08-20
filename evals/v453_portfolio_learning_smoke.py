from __future__ import annotations

from unittest.mock import patch

from portfolio_learning import portfolio_state
from risk_engine import evaluate_trade_candidate


class Client:
    testnet = False
    def get_positions(self, settle_coin="USDT"):
        return [
            {"symbol":"AAAUSDT","side":"Buy","size":"10","avgPrice":"1.0","stopLoss":"0.99"},
            {"symbol":"BBBUSDT","side":"Sell","size":"5","avgPrice":"2.0","stopLoss":"2.02"},
        ]


def main() -> None:
    with patch("portfolio_learning.symbol_correlation", return_value=0.20):
        state = portfolio_state(Client(), candidate_symbol="CCCUSDT", candidate_action="long", equity=100.0)
    assert state["open_positions"] == 2, state
    assert abs(float(state["estimated_open_risk_pct"]) - 0.20) < 1e-9, state
    assert not state["same_symbol_open"], state

    assessment={"action":"long","confidence":0.90,"entry":1.0,"stop_loss":0.99,"take_profit":1.02}
    snapshot={"symbol":"CCCUSDT","price":1.0,"spread_bps":1.0,"captured_at_ms":0}
    instrument={"lotSizeFilter":{"qtyStep":"1","minOrderQty":"1","minNotionalValue":"1"},"leverageFilter":{"maxLeverage":"10","leverageStep":"0.01"}}
    with patch("risk_engine.get_research_state", return_value="1"), patch("risk_engine.load_trading_settings") as cfg:
        cfg.return_value={
            "mode":"autopilot_live","min_confidence":0.60,"max_spread_bps":20,"max_data_age_seconds":90,
            "max_positions":5,"max_trades_per_day":20,"paper_start_equity":100,"max_daily_loss_pct":1.5,"max_weekly_loss_pct":4.0,
            "absolute_risk_cap_pct":0.75,"risk_per_trade_pct":0.25,"max_notional_pct_equity":100,"max_leverage":3,
            "max_notional_usdt":50,"executable_min_order_override":True,"min_order_override_max_risk_pct":0.45,
            "min_order_override_max_target_multiple":3,"portfolio_learning_enabled":True,"portfolio_learning_risk_cap_pct":0.75,
            "portfolio_absolute_risk_cap_pct":1.50,"portfolio_block_unprotected_positions":True,"min_reward_risk":1.30,
        }
        ok=evaluate_trade_candidate(assessment,snapshot,instrument,equity=100,open_positions=2,adaptive_risk_pct=0.25,
            max_positions_allowed=3,portfolio_open_risk_pct=0.20,portfolio_risk_cap_pct=0.75)
        assert ok["allowed"], ok
        assert ok["projected_portfolio_risk_pct"] <= 0.75, ok
        blocked=evaluate_trade_candidate(assessment,snapshot,instrument,equity=100,open_positions=3,adaptive_risk_pct=0.25,
            max_positions_allowed=3,portfolio_open_risk_pct=0.60,portfolio_risk_cap_pct=0.75)
        assert not blocked["allowed"], blocked
        assert any("maximum open positions" in x or "portfolio risk" in x for x in blocked["reasons"]), blocked

    print("v4.5.3 portfolio learning smoke: PASS")

if __name__ == "__main__":
    main()
