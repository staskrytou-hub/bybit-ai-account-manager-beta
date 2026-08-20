from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(tempfile.mkdtemp(prefix="stan-trading-eval-"))
os.environ["STAN_AI_HOME"] = str(TEMP)
sys.path.insert(0, str(ROOT))


def synthetic_rows(n: int = 1800) -> list[list[str]]:
    rows=[]; price=100.0
    for i in range(n):
        drift=0.0005 if (i//300)%2==0 else -0.0004
        price *= 1 + drift + 0.0018*math.sin(i/11)
        o=price*(1-0.0007); c=price; h=max(o,c)*1.0025; l=min(o,c)*0.9975; v=100+35*abs(math.sin(i/8))
        rows.append([str(1700000000000+i*900000),str(o),str(h),str(l),str(c),str(v),str(v*c)])
    return list(reversed(rows))


def main() -> int:
    from strategy_lab import STRATEGIES, evaluate_strategy_robustness
    from trading_config import apply_safe_autopilot_profile, audit_autopilot_growth_profile
    from risk_engine import evaluate_trade_candidate
    from research_store import set_research_state

    results=[]
    bt=evaluate_strategy_robustness(synthetic_rows(), STRATEGIES[0])
    results.append(("backtest_has_full_and_oos", bool(bt.get("full")) and bool(bt.get("out_of_sample"))))

    cfg=apply_safe_autopilot_profile(mode="autopilot_live", key_environment="mainnet_trade")
    results.append(("growth_profile_preflight", bool(audit_autopilot_growth_profile(cfg).get("passed"))))

    assessment={"action":"long","confidence":0.8,"entry":100.0,"stop_loss":99.5,"take_profit":101.0}
    snapshot={"symbol":"BTCUSDT","price":100.0,"spread_bps":1.0,"captured_at_ms":0}
    instrument={"lotSizeFilter":{"qtyStep":"0.001","minOrderQty":"0.001"},"leverageFilter":{"maxLeverage":"10","leverageStep":"0.01"}}
    risk=evaluate_trade_candidate(assessment,snapshot,instrument,equity=100,open_positions=0,adaptive_risk_pct=.15,leverage_cap=2,exposure_cap_pct=75)
    results.append(("live_blocked_before_bootstrap", any("bootstrap" in x for x in risk["reasons"])))

    set_research_state("bootstrap_complete","1")
    risk2=evaluate_trade_candidate(assessment,snapshot,instrument,equity=200,open_positions=0,adaptive_risk_pct=.35,leverage_cap=2,exposure_cap_pct=125,growth_stage="validated")
    results.append(("bootstrap_gate_removed", not any("bootstrap" in x for x in risk2["reasons"])))
    results.append(("no_fixed_50_live_cap", risk2["notional_usdt"] > 50.0))
    results.append(("minimal_leverage_selected", 1.0 <= risk2["leverage"] <= 2.0))

    print(json.dumps({"results":[{"name":n,"passed":p} for n,p in results]}, indent=2))
    return 0 if all(p for _,p in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
