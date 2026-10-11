"""Cost sensitivity of the accepted configuration on the holdout (information only — the
acceptance decision always uses 0.10% fee + 0.05% slippage per side).

    python scripts/research_costs.py   # appends "cost_sensitivity" to data/research/results.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import Backtester, build, compute_metrics, load_history  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.exits import ExitConfig  # noqa: E402
from bot.protections import ProtectionConfig  # noqa: E402
from bot.strategy import StrategyParams  # noqa: E402
from research import HOLDOUT, UNIVERSE, ts  # noqa: E402

CASES = [("Без издержек", 0.0, 0.0), ("Только комиссия maker 0.08%", 0.0008, 0.0),
         ("Maker 0.08% + проскальзывание 0.02%", 0.0008, 0.0002),
         ("Taker 0.10% + проскальзывание 0.05% (как в проверке)", 0.001, 0.0005)]


def main() -> int:
    cfg = load_config(ROOT / "config.example.yaml", with_secrets=False)
    res_path = ROOT / "data" / "research" / "results.json"
    res = json.loads(res_path.read_text(encoding="utf-8"))
    days = int((time.time() - ts(2019)) / 86400) + 2
    data = {s: load_history(s, days, offline=True) for s in UNIVERSE}
    start_hour = int(str(cfg["schedule"]["start_time"]).split(":")[0])
    base = StrategyParams.from_dict({"entry_mode": "pullback", "exit_mode": "fixed", "btc_regime": False})
    out = []
    for name, over in (("baseline", {}), ("final", res["accepted"])):
        d = {k: getattr(base, k) for k in base.__dataclass_fields__}
        d.update(over)
        strat = StrategyParams.from_dict(d)
        cache: dict = {}
        for label, fee, slip in CASES:
            params, engine = build(cfg, {}, max(fee, 1e-9))
            bt = Backtester(data, engine, params, strat, cfg.tz, start_hour, fee, slip,
                            ExitConfig.from_dict(cfg["trader"].get("exits")),
                            ProtectionConfig.from_dict(cfg["trader"].get("protections")), signal_cache=cache)
            m = compute_metrics(bt.run(label, HOLDOUT[0]))
            out.append({"config": name, "case": label, "fee": fee, "slippage": slip, "pnl": m.pnl, "trades": m.trades})
            print(f"{name:<9} {label:<55} PnL {m.pnl:+.3f} trades {m.trades}", flush=True)
    res["cost_sensitivity"] = out
    res_path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
