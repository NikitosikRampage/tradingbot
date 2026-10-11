"""Backtest of the strategy on OKX history with THE SAME session limits (RiskEngine),
the same exit barriers and the same protections as the live bot.

Examples:
    python backtest.py --days 365
    python backtest.py --days 365 --compare budget=10 hours=8 max_loss=2 target=1.5
    python backtest.py --walk-forward 4 --monte-carlo 5000 --per-pair
    python backtest.py --lookahead-check

Session parameters use the same key=value syntax as /session_start.
Costs: taker fee (>= 0.1% per side is enforced) + slippage on every fill.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import math
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from bot.config import ROOT, load_config
from bot.exits import ExitConfig, check_exit, raise_stop, stop_kind
from bot.indicators import resample
from bot.protections import ClosedTrade, ProtectionConfig, entry_blocked
from bot.risk import (STOP_REASON_RU, DailyCaps, DailyStats, HardCaps, Position, RiskEngine,
                      SessionParams, SessionStatus, StopReason)
from bot.strategy import Signal, StrategyParams, atr_pct_1h, exit_levels, generate_signal
from bot.trader import resolve_param

H = 3600_000
CACHE = ROOT / "data" / "backtest"
MIN_FEE = 0.001  # 0.1% per side — the minimum cost the user requires in every backtest


# ----------------------------------------------------------------------------- data
def load_history(symbol: str, days: int, warmup_hours: int = 900, refresh: bool = False,
                 offline: bool = False) -> list[list[float]]:
    """1h candles from OKX, cached in data/backtest/. ``offline`` = cache only (reproducible runs)."""
    import ccxt  # sync client is fine here

    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol.replace('/', '_')}_1h.csv"
    now = int(time.time() * 1000) // H * H
    start = now - (days * 24 + warmup_hours) * H
    rows: dict[int, list[float]] = {}
    if path.exists() and not refresh:
        with path.open(encoding="utf-8") as fh:
            for r in csv.reader(fh):
                rows[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:]]
    have = sorted(rows)
    if offline:
        if not rows:
            raise FileNotFoundError(f"нет кэша истории {path}")
        return [rows[k] for k in sorted(rows) if k >= start]
    from bot.exchange import net_options

    ex = ccxt.okx({"enableRateLimit": True, **net_options(sync=True)})
    ranges = []
    if not have or have[0] > start:
        ranges.append((start, have[0] if have else now))
    if have:
        ranges.append((have[-1] + H, now))
    for a, b in ranges:
        cursor = a
        while cursor < b:
            for attempt in range(6):
                try:
                    batch = ex.fetch_ohlcv(symbol, "1h", since=cursor, limit=100)
                    break
                except ccxt.NetworkError:  # incl. rate limits: back off and retry
                    if attempt == 5:
                        raise
                    time.sleep(2 ** attempt)
            batch = [x for x in batch if x[0] >= cursor]
            if not batch:
                cursor += 100 * H  # before the listing date: skip ahead
                continue
            for x in batch:
                if x[0] + H <= now:  # closed candles only
                    rows[int(x[0])] = [int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] or 0)]
            cursor = int(batch[-1][0]) + H
            print(f"\r  {symbol}: {datetime.fromtimestamp(cursor / 1000, timezone.utc):%Y-%m-%d}", end="", file=sys.stderr)
    print(file=sys.stderr)
    out = [rows[k] for k in sorted(rows) if k >= start]
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows[k] for k in sorted(rows))
    return out


# ----------------------------------------------------------------------------- results
@dataclass
class SessionResult:
    session_id: int
    start: float
    reason: str
    pnl: float
    fees: float
    trades: int
    wins: int
    max_dd: float


@dataclass
class TradeRow:
    session_id: int
    symbol: str
    entry_ts: float
    exit_ts: float
    entry: float
    exit: float
    pnl: float
    pnl_pct: float
    fees: float
    kind: str


@dataclass
class Result:
    name: str
    sessions: list[SessionResult] = field(default_factory=list)
    trades: list[TradeRow] = field(default_factory=list)


@dataclass
class Metrics:
    trades: int
    winrate: float
    pnl: float
    fees: float
    max_dd: float
    profit_factor: float
    expectancy: float
    sessions: int
    reasons: dict[str, int]


def max_drawdown(pnls: list[float]) -> float:
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def compute_metrics(r: Result) -> Metrics:
    tr = sorted(r.trades, key=lambda t: t.exit_ts)
    pnls = [t.pnl for t in tr]
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p <= 0)
    n = len(pnls)
    return Metrics(trades=n, winrate=(sum(1 for p in pnls if p > 0) / n * 100) if n else 0.0,
                   pnl=sum(pnls), fees=sum(t.fees for t in tr), max_dd=max_drawdown(pnls),
                   profit_factor=(gains / losses) if losses > 0 else (math.inf if gains > 0 else 0.0),
                   expectancy=(sum(pnls) / n) if n else 0.0, sessions=len(r.sessions),
                   reasons=dict(Counter(s.reason for s in r.sessions)))


def monte_carlo(pnls: list[float], runs: int = 5000, seed: int = 42) -> dict[str, float]:
    """Bootstrap the trade sequence (resample with replacement): distribution of total PnL and
    max drawdown. Shows how much of the result could be luck (Jesse-style Monte Carlo)."""
    if len(pnls) < 2:
        return {}
    rnd = random.Random(seed)
    totals, dds = [], []
    for _ in range(runs):
        s = rnd.choices(pnls, k=len(pnls))
        totals.append(sum(s))
        dds.append(max_drawdown(s))
    totals.sort()
    dds.sort()

    def q(xs: list[float], p: float) -> float:
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return {"pnl_p5": q(totals, 0.05), "pnl_p50": q(totals, 0.5), "pnl_p95": q(totals, 0.95),
            "dd_p50": q(dds, 0.5), "dd_p95": q(dds, 0.95),
            "prob_positive": sum(1 for t in totals if t > 0) / len(totals)}


def t_stat(pnls: list[float]) -> float:
    n = len(pnls)
    if n < 3:
        return 0.0
    m = sum(pnls) / n
    sd = math.sqrt(sum((p - m) ** 2 for p in pnls) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else 0.0


# ----------------------------------------------------------------------------- simulation
class Backtester:
    def __init__(self, data: dict[str, list[list[float]]], engine: RiskEngine, params: SessionParams,
                 strat: StrategyParams, tz: Any, start_hour: int, fee: float, slippage: float,
                 exits: ExitConfig | None = None, protections: ProtectionConfig | None = None,
                 continuous: bool = False, signal_cache: dict | None = None,
                 random_entry: tuple[float, int] | None = None):
        self.data = data
        self.engine = engine
        self.params = params
        self.strat = strat
        self.tz = tz
        self.start_hour = start_hour
        self.fee = fee
        self.slip = slippage
        self.exits = exits or ExitConfig()
        self.protections = protections or ProtectionConfig()
        self.continuous = continuous
        # signals depend only on (data, strategy, bar): share the cache between runs of one strategy
        self.signal_cache = signal_cache if signal_cache is not None else {}
        # control experiment: enter at random with probability p per bar (same exits/limits)
        self.random_entry = random_entry
        self.idx = {s: {int(r[0]): i for i, r in enumerate(rows)} for s, rows in data.items()}
        self.c4 = {s: resample(rows, "1h", "4h", drop_incomplete=False) for s, rows in data.items()}
        self.c4ts = {s: [int(r[0]) for r in c] for s, c in self.c4.items()}

    def closes_4h(self, s: str, bar_close_ms: int) -> list[float]:
        """4h candles that are fully closed at ``bar_close_ms`` (no lookahead)."""
        j = bisect.bisect_right(self.c4ts[s], bar_close_ms - 4 * H)
        return [r[4] for r in self.c4[s][max(0, j - (self.strat.ema_slow + 60)):j]]

    def signal_at(self, sym: str, i: int):
        per_sym = self.signal_cache.setdefault(sym, {})
        if i in per_sym:
            return per_sym[i]
        rows = self.data[sym]
        c1 = rows[max(0, i - 299):i + 1]
        close_ms = int(rows[i][0]) + H
        btc4 = None
        if self.strat.btc_regime and "BTC/USDT" in self.data:
            btc4 = self.closes_4h("BTC/USDT", close_ms)
        sig = generate_signal(sym, [r[4] for r in c1], self.closes_4h(sym, close_ms), self.strat,
                              candles_1h=c1, btc_closes_4h=btc4, fee_rate=self.fee)
        per_sym[i] = sig
        return sig

    def run(self, name: str, start_ts: float, end_ts: float | None = None) -> Result:
        res = Result(name)
        end_ms = (end_ts or float("inf")) * 1000
        timeline = sorted({int(r[0]) for rows in self.data.values() for r in rows
                           if start_ts * 1000 <= r[0] < end_ms})
        st = None
        sid = 0
        daily_realized: dict[str, float] = defaultdict(float)
        daily_budget: dict[str, float] = defaultdict(float)
        closed: list[ClosedTrade] = []
        open_meta: dict[str, float] = {}
        session_day_pnl: dict[tuple[int, str], float] = defaultdict(float)
        rnd = random.Random(self.random_entry[1]) if self.random_entry else None
        last_ts = timeline[-1] if timeline else 0
        for ts in timeline:
            bar_close = (ts + H) / 1000.0
            local = datetime.fromtimestamp(ts / 1000, self.tz)
            day = local.strftime("%Y-%m-%d")
            bars = {s: self.data[s][self.idx[s][ts]] for s in self.data if ts in self.idx[s]}

            due = (sid == 0) if self.continuous else (local.hour == self.start_hour)
            if st is None and due:
                if (daily_budget[day] + self.params.budget_usdt <= self.engine.daily_caps.daily_budget_cap_usdt
                        and daily_realized[day] > -self.engine.daily_caps.daily_loss_cap_usdt):
                    sid += 1
                    st = self.engine.new_session(sid, self.params, ts / 1000.0)
                    daily_budget[day] += self.params.budget_usdt
            if st is None:
                continue
            def close(sym: str, price: float, kind: str) -> None:
                pos = st.positions[sym]
                price *= (1 - self.slip)
                gross = pos.qty * price
                fee = gross * self.fee
                pnl = self.engine.record_close(st, sym, gross - fee, fee)
                daily_realized[day] += pnl
                session_day_pnl[(st.session_id, day)] += pnl
                pct = pnl / pos.cost_usdt * 100
                res.trades.append(TradeRow(st.session_id, sym, open_meta.pop(sym), bar_close, pos.entry_price,
                                           price, pnl, pct, pos.entry_fee_usdt + fee, kind))
                closed.append(ClosedTrade(sym, bar_close, pnl, pct, kind))

            # --- exits inside the bar (pessimistic order: stop first, then TP)
            for sym, pos in list(st.positions.items()):
                b = bars.get(sym)
                if not b:
                    continue
                o, h, low, c = b[1], b[2], b[3], b[4]
                if low <= pos.stop_loss:
                    close(sym, min(o, pos.stop_loss), stop_kind(pos))
                elif h >= pos.take_profit:
                    close(sym, max(o, pos.take_profit), "take_profit")
                else:
                    raise_stop(pos, h, self.exits, self.fee)        # effective from the next bar
                    hit = check_exit(pos, c, bar_close, self.exits)  # time limit at the close
                    if hit is not None and hit[0] == "time_limit":
                        close(sym, c, "time_limit")

            # --- session limits: worst case inside the bar (lows), then at the close
            for field_i in (3, 4):
                if st.status == SessionStatus.ENDED:
                    break
                prices = {s: b[field_i] for s, b in bars.items()}
                daily = DailyStats(daily_budget[day], daily_realized[day] - session_day_pnl[(st.session_id, day)])
                for a in self.engine.evaluate(st, prices, bar_close, daily):
                    if a.kind == "close_all":
                        for sym in list(st.positions):
                            close(sym, prices.get(sym, st.positions[sym].entry_price), "session")
                        self.engine.finish(st, a.reason, bar_close)

            # --- entries at the bar close
            if st.status != SessionStatus.ENDED:
                for sym, b in bars.items():
                    ok, _, size = self.engine.can_open(st, sym, bar_close)
                    if not ok:
                        continue
                    if entry_blocked(closed, self.protections, sym, bar_close)[0]:
                        continue
                    if rnd is not None:
                        i = self.idx[sym][ts]
                        if rnd.random() >= self.random_entry[0] or i < 300:
                            continue
                        sig = None
                        if self.strat.exit_mode == "atr":  # same exit rule as the strategy under test
                            a = atr_pct_1h(self.data[sym][i - 140:i + 1], self.strat.atr_period)
                            if a is None:
                                continue
                            sig = Signal(sym, b[4], "random", 0, 0, 0, 0, sl_pct=self.strat.atr_sl_mult * a,
                                         tp_pct=max(self.strat.atr_tp_mult * a, 6 * self.fee * 100 + 0.01))
                    else:
                        sig = self.signal_at(sym, self.idx[sym][ts])
                        if sig is None:
                            continue
                    price = b[4] * (1 + self.slip)
                    qty = size / price * (1 - self.fee)
                    p = st.params
                    stop, target = exit_levels(price, sig, p.stop_loss_pct, p.take_profit_pct)
                    self.engine.record_open(st, Position(
                        symbol=sym, qty=qty, entry_price=price, cost_usdt=size, entry_fee_usdt=size * self.fee,
                        stop_loss=stop, take_profit=target,
                        opened_at=bar_close, highest=price, initial_stop=stop))
                    open_meta[sym] = bar_close

            if st.status == SessionStatus.ENDED or ts == last_ts:
                if ts == last_ts and st.status != SessionStatus.ENDED:  # end of data/fold
                    for sym in list(st.positions):
                        close(sym, bars[sym][4] if sym in bars else st.positions[sym].entry_price, "session")
                    reason = "end_of_data"
                else:
                    reason = st.stop_reason.value if st.stop_reason else "?"
                res.sessions.append(SessionResult(st.session_id, st.started_at, reason, st.realized_pnl,
                                                  st.fees_paid, st.trades_closed, st.wins, st.max_drawdown))
                st = None
        return res


def lookahead_check(data: dict[str, list[list[float]]], strat: StrategyParams, samples: int = 300,
                    seed: int = 1, cls: type = None) -> list[str]:
    """Freqtrade-style lookahead analysis: every signal computed during the backtest (with
    indicators prepared on the FULL history) must be identical to the signal computed from the
    history truncated at that bar. Returns a list of mismatches (empty = no lookahead bias)."""
    cls = cls or Backtester
    bt = cls(data, RiskEngine(HardCaps(), DailyCaps()), SessionParams(), strat, None, 0, MIN_FEE, 0)
    rnd = random.Random(seed)
    problems = []
    for sym, rows in data.items():
        start = strat.ema_slow * 4 + 50
        if len(rows) <= start + 1:
            continue
        idxs = sorted(rnd.sample(range(start, len(rows)), min(samples, len(rows) - start)))
        for i in idxs:
            full = bt.signal_at(sym, i)
            cut_ts = rows[i][0]   # truncate EVERY symbol at the same moment (BTC regime filter)
            trunc = {s2: (r2[:i + 1] if s2 == sym else [r for r in r2 if r[0] <= cut_ts])
                     for s2, r2 in data.items()}
            tb = cls(trunc, bt.engine, bt.params, strat, None, 0, MIN_FEE, 0)
            cut = tb.signal_at(sym, i)
            same = (full is None and cut is None) or (
                full is not None and cut is not None
                and all(abs(getattr(full, k) - getattr(cut, k)) <= 1e-9 * max(1.0, abs(getattr(cut, k)))
                        for k in ("ema_fast", "ema_slow", "rsi_prev", "rsi_now")))
            if not same:
                problems.append(f"{sym} bar {datetime.fromtimestamp(rows[i][0] / 1000, timezone.utc):%Y-%m-%d %H:%M}")
    return problems


# ----------------------------------------------------------------------------- reporting
def _reason_ru(value: str) -> str:
    try:
        return STOP_REASON_RU[StopReason(value)]
    except ValueError:
        return value


def summarize(r: Result, budget: float, mc_runs: int = 0) -> str:
    m = compute_metrics(r)
    pf = "∞" if m.profit_factor == math.inf else f"{m.profit_factor:.2f}"
    lines = [f"=== {r.name} ===",
             f"Сессий: {m.sessions}, сделок: {m.trades}, винрейт: {m.winrate:.1f}%, profit factor: {pf}",
             f"PnL (с комиссиями и проскальзыванием): {m.pnl:+.4f} USDT ({m.pnl / budget * 100:+.2f}% бюджета), "
             f"комиссии: {m.fees:.4f} USDT, ожидание/сделку: {m.expectancy:+.4f}",
             f"Макс. просадка (по сделкам): {m.max_dd:.4f} USDT"]
    kinds = Counter(t.kind for t in r.trades)
    if kinds:
        lines.append("Выходы: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
    if m.reasons:
        lines.append("Причины остановки сессий: " + ", ".join(
            f"{_reason_ru(k)}={v}" for k, v in sorted(m.reasons.items(), key=lambda kv: -kv[1])))
    if mc_runs and len(r.trades) >= 2:
        mc = monte_carlo([t.pnl for t in r.trades], mc_runs)
        lines.append(f"Monte Carlo ({mc_runs} бутстрепов): PnL p5/p50/p95 = {mc['pnl_p5']:+.3f}/"
                     f"{mc['pnl_p50']:+.3f}/{mc['pnl_p95']:+.3f}, P(PnL>0) = {mc['prob_positive'] * 100:.0f}%, "
                     f"просадка p95 = {mc['dd_p95']:.3f}")
    return "\n".join(lines)


def per_pair_table(r: Result) -> str:
    by: dict[str, list[TradeRow]] = defaultdict(list)
    for t in r.trades:
        by[t.symbol].append(t)
    lines = ["Пара        сделок  винрейт   PnL USDT  ожид./сделку  t-stat"]
    for sym, ts in sorted(by.items(), key=lambda kv: -sum(t.pnl for t in kv[1])):
        p = [t.pnl for t in ts]
        lines.append(f"{sym:<11} {len(p):>6}  {sum(1 for x in p if x > 0) / len(p) * 100:>6.0f}%  "
                     f"{sum(p):>+9.4f}  {sum(p) / len(p):>+12.4f}  {t_stat(p):>+6.2f}")
    lines.append("(|t| < 2 — результат статистически неотличим от нуля)")
    return "\n".join(lines)


def build(cfg: Any, overrides: dict[str, Any], fee: float | None) -> tuple[SessionParams, RiskEngine]:
    params = cfg.session_defaults.replace(**overrides)
    fee = cfg.taker_fee if fee is None else fee
    return params, RiskEngine(cfg.hard_caps, cfg.daily_caps, fee)


def unlimited_variant(params: SessionParams, days: int, fee: float) -> tuple[SessionParams, RiskEngine]:
    big = 1e12
    caps = HardCaps(max_budget_usdt=big, max_loss_usdt=big, max_duration_hours=big,
                    max_trades_per_session=10 ** 9, max_position_size_pct=params.position_size_pct,
                    max_open_positions=params.max_open_positions)
    free = params.replace(duration_hours=days * 24 + 48, max_loss_usdt=None, max_loss_pct=None,
                          profit_target_usdt=None, profit_target_pct=None, profit_lock=False,
                          max_trades=10 ** 8, max_consecutive_losses=0)
    return free, RiskEngine(caps, DailyCaps(big, big), fee)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("params", nargs="*", help="key=value как в /session_start")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--start-hour", type=int, default=None, help="час старта сессии (локальное время)")
    ap.add_argument("--fee", type=float, default=None, help="комиссия на сторону (>= 0.001)")
    ap.add_argument("--slippage", type=float, default=0.0005)
    ap.add_argument("--compare", action="store_true", help="также прогнать без лимитов сессии")
    ap.add_argument("--walk-forward", type=int, default=0, metavar="K", help="разбить период на K окон")
    ap.add_argument("--monte-carlo", type=int, default=2000, metavar="N", help="число бутстрепов (0 = выкл)")
    ap.add_argument("--per-pair", action="store_true", help="таблица результатов по парам")
    ap.add_argument("--lookahead-check", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="перекачать историю")
    ap.add_argument("--offline", action="store_true", help="только кэш data/backtest (без сети)")
    ap.add_argument("--trades-csv", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config, with_secrets=False)
    overrides = {}
    for kv in args.params:
        k, v = kv.split("=", 1)
        overrides[resolve_param(k)] = v
    params, engine = build(cfg, overrides, args.fee)
    if engine.fee_rate < MIN_FEE:
        print(f"Комиссия {engine.fee_rate} < {MIN_FEE}: по требованиям бэктест считается минимум с 0.1% на сторону.")
        return 2
    errs = engine.validate_params(params)
    if errs:
        print("Параметры нарушают hard_caps:\n- " + "\n- ".join(errs))
        return 2
    strat = StrategyParams.from_dict(cfg["trader"]["strategy"])
    exits = ExitConfig.from_dict(cfg["trader"].get("exits"))
    prot = ProtectionConfig.from_dict(cfg["trader"].get("protections"))
    symbols = [s.upper() for s in (args.symbols or cfg.symbols)]
    start_hour = args.start_hour if args.start_hour is not None else int(str(cfg["schedule"]["start_time"]).split(":")[0])
    print(f"Загрузка истории 1h за {args.days} дн. (+прогрев для EMA200 на 4h)...", file=sys.stderr)
    data = {s: load_history(s, args.days, refresh=args.refresh, offline=args.offline) for s in symbols}

    if args.lookahead_check:
        problems = lookahead_check(data, strat)
        print("Lookahead: " + ("смещения не найдено ✅" if not problems else f"НАЙДЕНО {len(problems)}: {problems[:5]}"))

    start_ts = time.time() - args.days * 86400
    bt = Backtester(data, engine, params, strat, cfg.tz, start_hour, engine.fee_rate, args.slippage, exits, prot)
    r = bt.run(f"С лимитами сессии (старт {start_hour:02d}:00, {params.duration_hours:g}ч, бюджет "
               f"{params.budget_usdt:g}, комиссия {engine.fee_rate * 100:.2f}% + проскальзывание "
               f"{args.slippage * 100:.2f}% на сторону)", start_ts)
    print(summarize(r, params.budget_usdt, args.monte_carlo))
    if args.per_pair:
        print("\n" + per_pair_table(r))
    if args.walk_forward > 1:
        k = args.walk_forward
        span = (time.time() - start_ts) / k
        print(f"\nWalk-forward: {k} последовательных окон по {span / 86400:.0f} дн.")
        for i in range(k):
            a = start_ts + i * span
            ri = bt.run(f"окно {i + 1}", a, a + span)
            mi = compute_metrics(ri)
            print(f"  окно {i + 1} ({datetime.fromtimestamp(a):%Y-%m-%d}): сделок {mi.trades}, PnL {mi.pnl:+.4f}, "
                  f"винрейт {mi.winrate:.0f}%, просадка {mi.max_dd:.4f}")
    if args.trades_csv:
        with open(args.trades_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["session", "symbol", "entry_time", "exit_time", "entry", "exit", "pnl", "pnl_pct", "fees", "kind"])
            for t in r.trades:
                w.writerow([t.session_id, t.symbol, datetime.fromtimestamp(t.entry_ts, cfg.tz).isoformat(),
                            datetime.fromtimestamp(t.exit_ts, cfg.tz).isoformat(), t.entry, t.exit,
                            round(t.pnl, 6), round(t.pnl_pct, 4), round(t.fees, 6), t.kind])
    if args.compare:
        free, e2 = unlimited_variant(params, args.days, engine.fee_rate)
        bt2 = Backtester(data, e2, free, strat, cfg.tz, start_hour, engine.fee_rate, args.slippage, exits, prot,
                         continuous=True)
        print()
        print(summarize(bt2.run("Без лимитов сессии (только SL/TP, торговля 24/7)", start_ts),
                        params.budget_usdt, args.monte_carlo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
