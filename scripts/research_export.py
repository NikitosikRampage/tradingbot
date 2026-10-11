"""Turn data/research/results.json into flat CSV tables (one per chart of the report).

    python scripts/research_export.py [--in data/research/results.json] [--out data/research]
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LABELS = {
    "baseline": "Текущая стратегия",
    "atr_exits": "H1: SL/TP по ATR",
    "min_atr": "H2: фильтр ATR ≥ 0.5%",
    "btc_regime": "H3: фильтр режима BTC",
    "breakout_atr": "H4: пробой + ATR-выходы",
    "final": "Принятая комбинация",
}


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def r4(x):
    return None if x is None else round(float(x), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(ROOT / "data" / "research" / "results.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "research"))
    a = ap.parse_args()
    res = json.loads(Path(a.inp).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    # development period: every hypothesis vs the baseline
    rows = []
    for name, d in res["dev"].items():
        m = d["metrics"]
        y = d["years"]
        rows.append([name, LABELS.get(name, name), r4(m["pnl"]), round(m["winrate"], 1), r4(m["max_dd"]),
                     m["trades"], r4(m["fees"]), *[r4(v) for v in y], d.get("better_years", ""),
                     {True: "принята", False: "отклонена"}.get(d.get("accepted"), "база" if name == "baseline" else "итог")])
    write(out / "dev_variants.csv", ["variant", "label", "pnl", "winrate", "max_dd", "trades", "fees",
                                     "y2019", "y2020", "y2021", "y2022", "better_years", "decision"], rows)

    # equity curves 2019 → today, one point per day (last value of the day)
    rows = []
    for name, pts in res["curves"].items():
        last: dict[str, float] = {}
        for t, eq in pts:
            last[day(t)] = eq
        rows += [[d, name, LABELS.get(name, name), round(v, 4)] for d, v in sorted(last.items())]
    write(out / "equity.csv", ["date", "config", "label", "equity"], rows)

    # calendar years
    rows = []
    for name, ys in res["windows"].items():
        rows += [[y, name, LABELS.get(name, name), r4(v)] for y, v in ys]
    write(out / "yearly.csv", ["year", "config", "label", "pnl"], rows)

    # holdout
    rows = []
    for name, h in res["holdout"].items():
        for per, key in (("2023 → сегодня", "metrics"), ("2023-01 … 2024-09 (чистый)", "clean"),
                         ("2024-10 → сегодня", "recent")):
            m = h[key]
            rows.append([name, LABELS.get(name, name), per, r4(m["pnl"]), round(m["winrate"], 1), r4(m["max_dd"]),
                         m["trades"], r4(m["fees"]), r4(m["profit_factor"])])
    write(out / "holdout.csv", ["config", "label", "period", "pnl", "winrate", "max_dd", "trades", "fees",
                                "profit_factor"], rows)

    # per pair (holdout, final configuration)
    rows = [[p["symbol"], p["trades"], r4(p["pnl"]), round(p["winrate"], 1), round(p["t"], 2)]
            for p in res["holdout"]["final"]["per_pair"]]
    write(out / "pairs.csv", ["symbol", "trades", "pnl", "winrate", "t_stat"], rows)

    # exits
    rows = []
    for name, h in res["holdout"].items():
        rows += [[name, LABELS.get(name, name), k, v] for k, v in sorted(h["exits"].items())]
    write(out / "exits.csv", ["config", "label", "kind", "count"], rows)

    # random-entry control and Monte Carlo
    rc = res["random_control"]
    write(out / "random_control.csv", ["seed", "pnl"], [[i + 1, r4(v)] for i, v in enumerate(rc["pnls"])])
    write(out / "monte_carlo.csv", ["run", "pnl"], [[i + 1, v] for i, v in enumerate(res["holdout"]["final"]["mc_samples"])])

    # cost sensitivity (information only)
    if res.get("cost_sensitivity"):
        write(out / "costs.csv", ["config", "label", "case", "pnl", "trades"],
              [[c["config"], LABELS.get(c["config"], c["config"]), c["case"], r4(c["pnl"]), c["trades"]]
               for c in res["cost_sensitivity"]])

    # headline numbers
    hf, hb = res["holdout"]["final"], res["holdout"]["baseline"]
    mc = hf["mc"] or {}
    rows = [
        ["holdout_pnl_final", r4(hf["metrics"]["pnl"])],
        ["holdout_pnl_baseline", r4(hb["metrics"]["pnl"])],
        ["holdout_trades_final", hf["metrics"]["trades"]],
        ["holdout_maxdd_final", r4(hf["metrics"]["max_dd"])],
        ["holdout_fees_final", r4(hf["metrics"]["fees"])],
        ["mc_prob_positive", r4(mc.get("prob_positive"))],
        ["mc_p5", r4(mc.get("pnl_p5"))],
        ["mc_p95", r4(mc.get("pnl_p95"))],
        ["random_percentile", r4(rc["percentile"])],
        ["random_median", r4(sorted(rc["pnls"])[len(rc["pnls"]) // 2])],
        ["strategy_pnl", r4(rc["strategy_pnl"])],
        ["no_limits_holdout_pnl", r4(res["no_limits_holdout"]["pnl"])],
        ["pairs", len(res["universe"])],
        ["budget", res["session"]["budget"]],
    ]
    write(out / "summary.csv", ["metric", "value"], rows)
    print("written:", ", ".join(sorted(p.name for p in out.glob("*.csv"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
