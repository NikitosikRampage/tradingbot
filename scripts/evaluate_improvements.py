"""Decide which improvements stay ON: each variant vs the baseline on real OKX history,
in-sample (first part) / out-of-sample (last part) + walk-forward windows, with fees and
slippage. A variant is accepted only if it is not worse in-sample AND better out-of-sample
AND better in at least half of the walk-forward windows AND does not increase the OOS drawdown
by more than 25%.

    python scripts/evaluate_improvements.py --days 365
    python scripts/evaluate_improvements.py --days 365 --offline --out data/backtest/evaluation.md
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest import (MIN_FEE, Backtester, Metrics, build, compute_metrics, load_history,  # noqa: E402
                      monte_carlo, per_pair_table, unlimited_variant)
from bot.config import load_config  # noqa: E402
from bot.exits import ExitConfig  # noqa: E402
from bot.protections import ProtectionConfig  # noqa: E402
from bot.strategy import StrategyParams  # noqa: E402

VARIANTS: dict[str, tuple[dict, dict]] = {
    # name: (exits, protections)
    "baseline": ({}, {}),
    "cooldown_4h": ({}, {"enabled": True, "cooldown_minutes": 240}),
    "stoploss_guard": ({}, {"enabled": True, "stoploss_guard": {
        "enabled": True, "lookback_hours": 24, "trade_limit": 2, "stop_minutes": 360}}),
    "low_profit_pairs": ({}, {"enabled": True, "low_profit_pairs": {
        "enabled": True, "lookback_hours": 72, "trade_limit": 2, "required_profit_pct": 0, "stop_minutes": 1440}}),
    "max_drawdown": ({}, {"enabled": True, "max_drawdown": {
        "enabled": True, "lookback_hours": 72, "trade_limit": 3, "max_drawdown_usdt": 1.0, "stop_minutes": 720}}),
    "trailing_2_1.5": ({"trailing_enabled": True, "trailing_activation_pct": 2, "trailing_distance_pct": 1.5}, {}),
    "breakeven_1.5": ({"breakeven_enabled": True, "breakeven_activation_pct": 1.5}, {}),
    "time_limit_6h": ({"time_limit_hours": 6}, {}),
}


@dataclass
class Row:
    name: str
    is_m: Metrics
    oos_m: Metrics
    folds: list[float]
    accepted: bool | None = None
    note: str = ""


def fmt(m: Metrics) -> str:
    return f"{m.pnl:+.3f} | {m.winrate:.0f}% | {m.max_dd:.3f} | {m.trades} | {m.fees:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--split", type=float, default=0.6, help="доля in-sample")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--fee", type=float, default=MIN_FEE)
    ap.add_argument("--slippage", type=float, default=0.0005)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "backtest" / "evaluation.md"))
    args = ap.parse_args()
    if args.fee < MIN_FEE:
        print("fee < 0.1% на сторону запрещено")
        return 2

    cfg = load_config(args.config, with_secrets=False)
    params, engine = build(cfg, {}, args.fee)
    strat = StrategyParams.from_dict(cfg["trader"]["strategy"])
    symbols = [s.upper() for s in (args.symbols or cfg.symbols)]
    data = {s: load_history(s, args.days, offline=args.offline) for s in symbols}
    start_hour = int(str(cfg["schedule"]["start_time"]).split(":")[0])
    t0 = time.time() - args.days * 86400
    t_split = t0 + (time.time() - t0) * args.split
    span = (time.time() - t0) / args.folds

    def bt_for(exits: dict, prot: dict, eng=engine, prm=params, continuous=False) -> Backtester:
        return Backtester(data, eng, prm, strat, cfg.tz, start_hour, args.fee, args.slippage,
                          ExitConfig.from_dict(exits), ProtectionConfig.from_dict(prot), continuous)

    rows: list[Row] = []
    for name, (ex, pr) in VARIANTS.items():
        bt = bt_for(ex, pr)
        is_m = compute_metrics(bt.run(name, t0, t_split))
        oos_m = compute_metrics(bt.run(name, t_split))
        folds = [compute_metrics(bt.run(name, t0 + i * span, t0 + (i + 1) * span)).pnl for i in range(args.folds)]
        rows.append(Row(name, is_m, oos_m, folds))
        print(f"{name:<18} IS {fmt(is_m)}   OOS {fmt(oos_m)}   folds {[round(f, 3) for f in folds]}", flush=True)

    base = rows[0]
    accepted_ex: dict = {}
    accepted_pr: dict = {"enabled": False}
    for r in rows[1:]:
        better_folds = sum(1 for a, b in zip(r.folds, base.folds) if a > b + 1e-9)
        same = all(abs(a - b) < 1e-9 for a, b in zip(r.folds, base.folds)) and abs(r.oos_m.pnl - base.oos_m.pnl) < 1e-9
        ok = (r.is_m.pnl >= base.is_m.pnl - 1e-9 and r.oos_m.pnl > base.oos_m.pnl + 1e-9
              and better_folds * 2 >= args.folds
              and r.oos_m.max_dd <= base.oos_m.max_dd * 1.25 + 0.05)
        r.accepted = ok
        r.note = ("не сработал ни разу (идентично базе)" if same else
                  f"лучше в {better_folds}/{args.folds} окнах")
        if ok:
            ex, pr = VARIANTS[r.name]
            accepted_ex.update(ex)
            if pr:
                accepted_pr["enabled"] = True
                accepted_pr.update({k: v for k, v in pr.items() if k != "enabled"})

    lines = [f"# Оценка улучшений ({time.strftime('%Y-%m-%d')})", "",
             f"Пары: {', '.join(symbols)}; период {args.days} дн.; IS = первые {args.split * 100:.0f}%, "
             f"OOS = последние {100 - args.split * 100:.0f}%; комиссия {args.fee * 100:.2f}% + проскальзывание "
             f"{args.slippage * 100:.2f}% на сторону; сессии {params.duration_hours:g}ч с {start_hour:02d}:00, "
             f"бюджет {params.budget_usdt:g} USDT.", "",
             "| Вариант | IS PnL | IS винрейт | IS просадка | IS сделок | IS комиссии | OOS PnL | OOS винрейт | "
             "OOS просадка | OOS сделок | OOS комиссии | окна (PnL) | решение |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        dec = "база" if r.accepted is None else ("✅ принять" if r.accepted else "❌ отклонить")
        lines.append(f"| {r.name} | {fmt(r.is_m)} | {fmt(r.oos_m)} | {', '.join(f'{f:+.2f}' for f in r.folds)} | "
                     f"{dec} {r.note} |")

    final_bt = bt_for(accepted_ex, accepted_pr)
    full_base = bt_for({}, {}).run("base", t0)
    full_final = final_bt.run("final", t0)
    mb, mf = compute_metrics(full_base), compute_metrics(full_final)
    mcb = monte_carlo([t.pnl for t in full_base.trades], 5000)
    mcf = monte_carlo([t.pnl for t in full_final.trades], 5000)
    free, e2 = unlimited_variant(params, args.days, args.fee)
    mu = compute_metrics(bt_for({}, {}, e2, free, True).run("nolimits", t0))
    lines += ["", f"Принятая конфигурация: exits={accepted_ex or '{}'}, protections={accepted_pr}", "",
              "| Весь период | PnL | винрейт | просадка | сделок | комиссии | P(PnL>0) Monte Carlo | PnL p5…p95 |",
              "|---|---|---|---|---|---|---|---|"]
    for nm, m, mc in (("До (база)", mb, mcb), ("После (принятое)", mf, mcf)):
        mcs = (f"{mc['prob_positive'] * 100:.0f}% | {mc['pnl_p5']:+.2f}…{mc['pnl_p95']:+.2f}" if mc else "— | —")
        lines.append(f"| {nm} | {m.pnl:+.3f} | {m.winrate:.0f}% | {m.max_dd:.3f} | {m.trades} | {m.fees:.3f} | {mcs} |")
    lines.append(f"| Без лимитов сессии (справочно) | {mu.pnl:+.3f} | {mu.winrate:.0f}% | {mu.max_dd:.3f} | "
                 f"{mu.trades} | {mu.fees:.3f} | — | — |")
    lines += ["", "## По парам (база, весь период)", "", "```", per_pair_table(full_base), "```"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nСохранено: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
