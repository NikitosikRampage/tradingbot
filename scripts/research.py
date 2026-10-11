"""Research run on the long OKX history (2019 → today) with a LOCKED holdout.

Protocol (fixed in code BEFORE the first run — do not edit it after looking at results):
* universe: 16 liquid OKX spot /USDT pairs (BTC ETH SOL XRP DOGE ADA LTC LINK AVAX BCH DOT
  TRX BNB ETC XLM ZEC); pairs enter the backtest when their history starts;
* development period 2019-01-01 … 2022-12-31 — never used for any decision before;
* holdout 2023-01-01 … today, evaluated ONCE for the final configuration. Note: 2024-10 … today
  was used earlier to accept the 4h entry cut-off and the 6h time limit (they are part of the
  baseline here), so the holdout is also reported split into 2023-01…2024-09 (fully clean) and
  2024-10…today;
* costs: taker fee 0.10% + slippage 0.05% per side on every fill;
* hypotheses (from the diagnosis "a +4% target does not fit a 6–8h session"):
    H1 atr_exits    — SL = min(session SL 2%, 2×ATR1h), TP = 3×ATR1h
    H2 min_atr      — skip entries with ATR1h < 0.5% (the move can't pay the costs)
    H3 btc_regime   — alts only while BTC is in a 4h uptrend
    H4 breakout_atr — breakout of the 20h high (instead of the RSI pullback) + ATR exits
* acceptance on the development period: total PnL ≥ baseline + 0.10 USDT AND better in at
  least 3 of the 4 calendar years AND max drawdown ≤ 1.25 × baseline + 0.05;
* control: random entries with the same exits/limits and a similar number of trades
  (40 seeds) — if the strategy is not better than chance, the signal has no value.

    python scripts/research.py              # writes data/research/results.json
    python scripts/research_report.py       # builds data/research/report.html with charts
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest import (MIN_FEE, Backtester, Result, build, compute_metrics, load_history,  # noqa: E402
                      monte_carlo, t_stat, unlimited_variant)
from bot.config import load_config  # noqa: E402
from bot.exits import ExitConfig  # noqa: E402
from bot.protections import ProtectionConfig  # noqa: E402
from bot.strategy import StrategyParams  # noqa: E402

UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT", "LTC/USDT", "LINK/USDT",
            "AVAX/USDT", "BCH/USDT", "DOT/USDT", "TRX/USDT", "BNB/USDT", "ETC/USDT", "XLM/USDT", "ZEC/USDT"]
DEFAULT5 = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]


def ts(y: int, m: int = 1, d: int = 1) -> float:
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


DEV = (ts(2019), ts(2023))
YEARS = [(ts(y), ts(y + 1)) for y in (2019, 2020, 2021, 2022)]
HOLDOUT = (ts(2023), None)
HOLDOUT_CLEAN = (ts(2023), ts(2024, 10))
HOLDOUT_RECENT = (ts(2024, 10), None)

HYPOTHESES: dict[str, dict[str, Any]] = {
    "atr_exits": {"exit_mode": "atr", "atr_sl_mult": 2.0, "atr_tp_mult": 3.0},
    "min_atr": {"min_atr_pct": 0.5},
    "btc_regime": {"btc_regime": True},
    "breakout_atr": {"entry_mode": "breakout", "breakout_bars": 20, "exit_mode": "atr",
                     "atr_sl_mult": 2.0, "atr_tp_mult": 3.0},
}
MIN_GAIN = 0.10
MIN_YEARS = 3


def metrics_dict(r: Result) -> dict[str, Any]:
    m = compute_metrics(r)
    return {"pnl": m.pnl, "winrate": m.winrate, "max_dd": m.max_dd, "trades": m.trades, "fees": m.fees,
            "profit_factor": None if m.profit_factor == float("inf") else m.profit_factor,
            "expectancy": m.expectancy}


def equity(r: Result) -> list[list[float]]:
    eq, out = 0.0, []
    for t in sorted(r.trades, key=lambda t: t.exit_ts):
        eq += t.pnl
        out.append([round(t.exit_ts), round(eq, 5)])
    return out


def per_pair(r: Result) -> list[dict[str, Any]]:
    by: dict[str, list[float]] = {}
    for t in r.trades:
        by.setdefault(t.symbol, []).append(t.pnl)
    return sorted(({"symbol": s, "trades": len(p), "pnl": sum(p), "winrate": sum(x > 0 for x in p) / len(p) * 100,
                    "t": t_stat(p)} for s, p in by.items()), key=lambda d: -d["pnl"])


def exits(r: Result) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in r.trades:
        out[t.kind] = out.get(t.kind, 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--out", default=str(ROOT / "data" / "research" / "results.json"))
    ap.add_argument("--slippage", type=float, default=0.0005)
    ap.add_argument("--random-seeds", type=int, default=40)
    ap.add_argument("--universe", nargs="*", help="(debug) override the pair universe")
    args = ap.parse_args()

    cfg = load_config(args.config, with_secrets=False)
    params, engine = build(cfg, {}, MIN_FEE)
    # the baseline is pinned to the ORIGINAL strategy, independent of what config.yaml enables now
    base_strat = StrategyParams.from_dict({"ema_fast": 50, "ema_slow": 200, "rsi_period": 14, "rsi_pullback": 35,
                                           "rsi_max_entry": 60, "entry_mode": "pullback", "exit_mode": "fixed",
                                           "min_atr_pct": 0, "btc_regime": False})
    exits_cfg = ExitConfig.from_dict(cfg["trader"].get("exits"))
    prot = ProtectionConfig.from_dict(cfg["trader"].get("protections"))
    start_hour = int(str(cfg["schedule"]["start_time"]).split(":")[0])
    days = int((time.time() - ts(2019)) / 86400) + 2
    universe = [u.upper() for u in args.universe] if args.universe else UNIVERSE
    data = {s: load_history(s, days, offline=True) for s in universe}
    now = time.time()
    t_begin = time.time()

    def strat_of(over: dict[str, Any]) -> StrategyParams:
        d = {k: getattr(base_strat, k) for k in base_strat.__dataclass_fields__}
        d.update(over)
        return StrategyParams.from_dict(d)

    caches: dict[str, dict] = {}

    def bt(over: dict[str, Any], key: str, universe: list[str] | None = None, eng=engine, prm=params,
           continuous: bool = False, random_entry=None) -> Backtester:
        dd = data if universe is None else {s: data[s] for s in universe}
        ck = key + ("" if universe is None else "|" + ",".join(universe))
        return Backtester(dd, eng, prm, strat_of(over), cfg.tz, start_hour, MIN_FEE, args.slippage,
                          exits_cfg, prot, continuous, signal_cache=caches.setdefault(ck, {}),
                          random_entry=random_entry)

    def log(msg: str) -> None:
        print(f"[{time.time() - t_begin:6.0f}s] {msg}", flush=True)

    out: dict[str, Any] = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "universe": universe, "costs": {"fee": MIN_FEE, "slippage": args.slippage},
        "session": {"hours": params.duration_hours, "start_hour": start_hour, "budget": params.budget_usdt,
                    "cutoff_h": params.no_entry_last_hours, "time_limit_h": exits_cfg.time_limit_hours,
                    "sl": params.stop_loss_pct, "tp": params.take_profit_pct},
        "periods": {"dev": DEV, "holdout": [HOLDOUT[0], now], "holdout_clean": HOLDOUT_CLEAN,
                    "holdout_recent": [HOLDOUT_RECENT[0], now]},
        "data": {s: [rows[0][0] / 1000, rows[-1][0] / 1000, len(rows)] for s, rows in data.items()},
        "protocol": {"min_gain": MIN_GAIN, "min_years": MIN_YEARS, "hypotheses": HYPOTHESES},
    }

    # ---------------------------------------------------------------- development period
    variants: dict[str, dict[str, Any]] = {"baseline": {}, **HYPOTHESES}
    dev: dict[str, Any] = {}
    for name, over in variants.items():
        b = bt(over, name)
        full = b.run(name, *DEV)
        years = [compute_metrics(b.run(name, a, z)).pnl for a, z in YEARS]
        dev[name] = {"metrics": metrics_dict(full), "years": years, "exits": exits(full)}
        log(f"dev {name:<14} PnL {dev[name]['metrics']['pnl']:+.3f} trades {dev[name]['metrics']['trades']} "
            f"years {[round(y, 2) for y in years]}")
        if name != "baseline":
            caches.pop(name, None)  # free memory; only baseline/final are reused later
    base = dev["baseline"]
    accepted: dict[str, Any] = {}
    for name in HYPOTHESES:
        d = dev[name]
        better_years = sum(1 for a, z in zip(d["years"], base["years"]) if a > z + 1e-9)
        ok = (d["metrics"]["pnl"] >= base["metrics"]["pnl"] + MIN_GAIN and better_years >= MIN_YEARS
              and d["metrics"]["max_dd"] <= base["metrics"]["max_dd"] * 1.25 + 0.05)
        d.update(accepted=ok, better_years=better_years)
        if ok:
            accepted.update(HYPOTHESES[name])
    # entry modes are exclusive: if breakout is accepted, its own exits come with it
    combo_name = "final"
    if accepted:
        b = bt(accepted, combo_name)
        full = b.run(combo_name, *DEV)
        years = [compute_metrics(b.run(combo_name, a, z)).pnl for a, z in YEARS]
        dev[combo_name] = {"metrics": metrics_dict(full), "years": years, "exits": exits(full),
                           "params": accepted}
        log(f"dev final {accepted} PnL {dev[combo_name]['metrics']['pnl']:+.3f}")
    out["dev"] = dev
    out["accepted"] = accepted

    # ---------------------------------------------------------------- holdout (once)
    final_over = accepted if accepted else {}
    hold: dict[str, Any] = {}
    for name, over in (("baseline", {}), ("final", final_over)):
        b = bt(over, name if name == "baseline" or accepted else "baseline")
        r = b.run(name, HOLDOUT[0])
        hold[name] = {"metrics": metrics_dict(r), "exits": exits(r), "per_pair": per_pair(r),
                      "clean": metrics_dict(b.run(name, *HOLDOUT_CLEAN)),
                      "recent": metrics_dict(b.run(name, HOLDOUT_RECENT[0])),
                      "mc": monte_carlo([t.pnl for t in r.trades], 5000),
                      "mc_samples": _mc_samples([t.pnl for t in r.trades])}
        log(f"holdout {name} PnL {hold[name]['metrics']['pnl']:+.3f} trades {hold[name]['metrics']['trades']}")
    out["holdout"] = hold

    # ---------------------------------------------------------------- whole period curves / windows
    curves, windows = {}, {}
    for name, over in (("baseline", {}), ("final", final_over)):
        b = bt(over, name if name == "baseline" or accepted else "baseline")
        r = b.run(name, ts(2019))
        curves[name] = equity(r)
        windows[name] = [[y, compute_metrics(b.run(name, ts(y), ts(y + 1))).pnl] for y in range(2019, 2027)]
        out.setdefault("full", {})[name] = {"metrics": metrics_dict(r), "per_pair": per_pair(r), "exits": exits(r)}
    out["curves"] = curves
    out["windows"] = windows

    # ---------------------------------------------------------------- 5 default pairs (continuity)
    out["default5"] = {}
    for name, over in (("baseline", {}), ("final", final_over)):
        b = bt(over, name + "5", universe=[u for u in DEFAULT5 if u in data])
        out["default5"][name] = {"holdout": metrics_dict(b.run(name, HOLDOUT[0])),
                                 "dev": metrics_dict(b.run(name, *DEV))}

    # ---------------------------------------------------------------- random-entry control (holdout)
    target = max(1, hold["final"]["metrics"]["trades"])
    prob = _calibrate(lambda p: len(bt(final_over, "rnd", random_entry=(p, 1)).run("r", HOLDOUT[0]).trades), target)
    rnd = [compute_metrics(bt(final_over, "rnd", random_entry=(prob, seed)).run("r", HOLDOUT[0])).pnl
           for seed in range(1, args.random_seeds + 1)]
    out["random_control"] = {"prob": prob, "pnls": rnd, "strategy_pnl": hold["final"]["metrics"]["pnl"],
                             "percentile": sum(1 for x in rnd if x < hold["final"]["metrics"]["pnl"]) / len(rnd)}
    log(f"random control: p={prob:.5f}, strategy percentile {out['random_control']['percentile'] * 100:.0f}%")

    # ---------------------------------------------------------------- no session limits (reference)
    free, e2 = unlimited_variant(params, days, MIN_FEE)
    r = bt(final_over, "nolim", eng=e2, prm=free, continuous=True).run("nolim", HOLDOUT[0])
    out["no_limits_holdout"] = metrics_dict(r)

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"saved {p}")
    return 0


def _mc_samples(pnls: list[float], runs: int = 2000) -> list[float]:
    import random
    if len(pnls) < 2:
        return []
    rnd = random.Random(42)
    return [round(sum(rnd.choices(pnls, k=len(pnls))), 4) for _ in range(runs)]


def _calibrate(count, target: int) -> float:
    lo, hi = 1e-5, 0.05
    for _ in range(12):
        mid = (lo * hi) ** 0.5
        if count(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo * hi) ** 0.5


if __name__ == "__main__":
    sys.exit(main())
