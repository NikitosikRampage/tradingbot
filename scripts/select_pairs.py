"""Show which pairs the dynamic pairlist would select right now (public OKX data, no keys).

    python scripts/select_pairs.py            # uses pairlist settings from config.yaml
    python scripts/select_pairs.py --all      # also print rejected pairs with reasons
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ccxt.async_support as ccxt  # noqa: E402

from bot.config import load_config  # noqa: E402
from bot.pairlist import PairlistConfig, PairSelector, select_pairs  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, with_secrets=False)
    pl = PairlistConfig.from_dict(cfg.raw.get("pairlist"))
    pos = cfg.session_defaults.budget_usdt * cfg.session_defaults.position_size_pct / 100
    ex = ccxt.okx({"enableRateLimit": True})
    try:
        metrics = await PairSelector(pl, ex, pos).evaluate()
    finally:
        await ex.close()
    chosen = select_pairs(metrics, pl)
    by = {m.symbol: m for m in metrics}
    print(f"Выбрано ({len(chosen)} из max {pl.max_pairs}):")
    for s in chosen:
        print("  ✅ " + by[s].short())
    print("\nПрошли фильтры, но не вошли (по объёму):")
    for m in sorted((m for m in metrics if m.passed and m.symbol not in chosen), key=lambda m: -m.quote_volume)[:10]:
        print("  · " + m.short())
    deep = [m for m in metrics if not m.passed and m.spread_pct is not None]
    print(f"\nОтклонены после детальной проверки ({len(deep)}):")
    for m in sorted(deep, key=lambda m: -m.quote_volume):
        print(f"  ✖ {m.symbol}: {'; '.join(m.reasons)}")
    if args.all:
        for m in sorted((m for m in metrics if m.spread_pct is None), key=lambda m: -m.quote_volume):
            print(f"  ✖ {m.symbol}: {'; '.join(m.reasons)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
