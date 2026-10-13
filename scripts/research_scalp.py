"""5-minute scalping strategies on 1 year of OKX 5m candles (16 pairs). Protocol fixed in code
BEFORE the first run:

* data: last 365 days of 5m candles (download: backtest.load_history(sym, 365, tf="5m"));
* development = first 8 months, holdout = last 4 months (evaluated once, for information when
  nothing is accepted);
* sessions: 24 h from 00:00, no new entries in the last hour, positions closed after 1 h
  (time limit), default loss limits, up to 50 trades per session (hard cap);
* costs: taker fee 0.10% + slippage 0.05% per side on every fill;
* S1 scalp_revert  — close below Bollinger(20, 2σ) and RSI(14) < 30 inside a 1h uptrend;
                      SL 1.5×ATR(5m) (≤ session SL), TP max(1.5×ATR, 3× round-trip fee)
* S2 scalp_breakout — close above the 12-candle high with volume ≥ 1.5× average, 1h uptrend;
                      SL 1×ATR(5m), TP max(2×ATR, 3× round-trip fee)
* acceptance: development PnL ≥ 0 after costs AND positive in at least 2 of the 4 two-month
  windows of the development period;
* control: random entries with the same exits/limits and a similar number of trades (20 seeds).

    python scripts/research_scalp.py      # -> data/research/scalp_results.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import MIN_FEE, Backtester, build, compute_metrics, load_history, monte_carlo  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.exits import ExitConfig  # noqa: E402
from bot.protections import ProtectionConfig  # noqa: E402
from bot.strategy import SCALP_PRESETS, SCALP_SESSION, SCALP_TIME_LIMIT_HOURS, StrategyParams  # noqa: E402
from research import UNIVERSE, _calibrate, exits, metrics_dict, per_pair  # noqa: E402

SLIP = 0.0005
SESSION = dict(SCALP_SESSION)
EXITS = {"time_limit_hours": SCALP_TIME_LIMIT_HOURS}
VARIANTS: dict[str, dict[str, Any]] = SCALP_PRESETS  # the same presets the UI switches to on DEMO
COSTS = [("Без издержек", 0.0, 0.0), ("Maker 0.08% + проскальзывание 0.02%", 0.0008, 0.0002),
         ("Taker 0.10% + проскальзывание 0.05% (как в проверке)", MIN_FEE, SLIP)]


def main() -> int:
    cfg = load_config(ROOT / "config.example.yaml", with_secrets=False)
    data = {s: load_history(s, 365, warmup_hours=24 * 20, offline=True, tf="5m") for s in UNIVERSE}
    end = max(rows[-1][0] for rows in data.values()) / 1000 + 300
    start = end - 365 * 86400
    split = start + (end - start) * 8 / 12
    windows = [(start + i * (split - start) / 4, start + (i + 1) * (split - start) / 4) for i in range(4)]
    t0 = time.time()

    def log(m: str) -> None:
        print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)

    def bt(over: dict[str, Any], cache: dict, fee: float = MIN_FEE, slip: float = SLIP, random_entry=None) -> Backtester:
        params, engine = build(cfg, SESSION, max(fee, 1e-9))
        strat = StrategyParams.from_dict(over)
        return Backtester(data, engine, params, strat, cfg.tz, 0, fee, slip, ExitConfig.from_dict(EXITS),
                          ProtectionConfig(), signal_cache=cache, random_entry=random_entry)

    out: dict[str, Any] = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "universe": UNIVERSE,
                           "periods": {"dev": [start, split], "holdout": [split, end]}, "session": SESSION,
                           "exits": EXITS, "variants": VARIANTS, "dev": {}, "holdout": {}, "cost_sensitivity": []}
    caches = {k: {} for k in VARIANTS}
    for name, over in VARIANTS.items():
        b = bt(over, caches[name])
        r = b.run(name, start, split)
        wins = [compute_metrics(b.run(name, a, z)).pnl for a, z in windows]
        m = metrics_dict(r)
        ok = m["pnl"] >= 0 and sum(1 for w in wins if w > 0) >= 2
        out["dev"][name] = {"metrics": m, "windows": wins, "exits": exits(r), "accepted": ok,
                            "trades_per_day": m["trades"] / ((split - start) / 86400)}
        log(f"dev {name:<15} PnL {m['pnl']:+.3f} trades {m['trades']} ({out['dev'][name]['trades_per_day']:.1f}/день) "
            f"winrate {m['winrate']:.0f}% fees {m['fees']:.2f} windows {[round(w, 2) for w in wins]} -> {'ACCEPT' if ok else 'reject'}")
    for name, over in VARIANTS.items():
        b = bt(over, caches[name])
        r = b.run(name, split, end)
        out["holdout"][name] = {"metrics": metrics_dict(r), "exits": exits(r), "per_pair": per_pair(r),
                                "mc": monte_carlo([t.pnl for t in r.trades], 3000)}
        log(f"holdout {name:<15} PnL {out['holdout'][name]['metrics']['pnl']:+.3f} trades {out['holdout'][name]['metrics']['trades']}")
        for label, fee, slip in COSTS:
            m = compute_metrics(bt(over, caches[name], fee, slip).run(label, split, end))
            out["cost_sensitivity"].append({"config": name, "case": label, "pnl": m.pnl, "trades": m.trades})
            log(f"   costs {label:<52} PnL {m.pnl:+.3f}")
    # random control for the strategy with more trades
    best = max(VARIANTS, key=lambda k: out["holdout"][k]["metrics"]["pnl"])
    target = max(1, out["holdout"][best]["metrics"]["trades"])
    prob = _calibrate(lambda p: len(bt(VARIANTS[best], {}, random_entry=(p, 1)).run("r", split, end).trades), target)
    rnd = [compute_metrics(bt(VARIANTS[best], {}, random_entry=(prob, s)).run("r", split, end)).pnl for s in range(1, 21)]
    sp = out["holdout"][best]["metrics"]["pnl"]
    out["random_control"] = {"config": best, "prob": prob, "pnls": rnd, "strategy_pnl": sp,
                             "percentile": sum(1 for x in rnd if x < sp) / len(rnd)}
    log(f"random control ({best}): strategy better than {out['random_control']['percentile'] * 100:.0f}% of random runs")
    p = ROOT / "data" / "research" / "scalp_results.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"saved {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
