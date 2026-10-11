"""Backtest of the strategy on OKX history with THE SAME session limits (RiskEngine).

Examples:
    python backtest.py --days 180
    python backtest.py --days 365 --compare budget=10 hours=8 max_loss=2 target=1.5
    python backtest.py --symbols BTC/USDT ETH/USDT --start-hour 9 lock=on trail=30 end=manage_only

Session parameters use the same key=value syntax as /session_start.
``--compare`` additionally runs the strategy without session limits (only SL/TP) to show
how the limits change the result.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from bot.config import ROOT, load_config
from bot.indicators import resample
from bot.risk import (STOP_REASON_RU, DailyCaps, DailyStats, HardCaps, Position, RiskEngine,
                      SessionParams, SessionStatus, StopReason)
from bot.strategy import StrategyParams, generate_signal
from bot.trader import resolve_param

H = 3600_000
CACHE = ROOT / "data" / "backtest"


# ----------------------------------------------------------------------------- data
def load_history(symbol: str, days: int, warmup_hours: int = 900, refresh: bool = False) -> list[list[float]]:
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
    ex = ccxt.okx({"enableRateLimit": True})
    ranges = []
    if not have or have[0] > start:
        ranges.append((start, have[0] if have else now))
    if have:
        ranges.append((have[-1] + H, now))
    for a, b in ranges:
        cursor = a
        while cursor < b:
            batch = ex.fetch_ohlcv(symbol, "1h", since=cursor, limit=100)
            batch = [x for x in batch if x[0] >= cursor]
            if not batch:
                break
            for x in batch:
                if x[0] + H <= now:  # closed candles only
                    rows[int(x[0])] = [int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] or 0)]
            cursor = int(batch[-1][0]) + H
            print(f"\r  {symbol}: {datetime.utcfromtimestamp(cursor / 1000):%Y-%m-%d}", end="", file=sys.stderr)
    print(file=sys.stderr)
    out = [rows[k] for k in sorted(rows) if k >= start]
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows[k] for k in sorted(rows))
    return out


# ----------------------------------------------------------------------------- simulation
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
    reason: str


@dataclass
class Result:
    name: str
    sessions: list[SessionResult] = field(default_factory=list)
    trades: list[TradeRow] = field(default_factory=list)
    equity_peak: float = 0.0
    equity_max_dd: float = 0.0
    equity: float = 0.0


class Backtester:
    def __init__(self, data: dict[str, list[list[float]]], engine: RiskEngine, params: SessionParams,
                 strat: StrategyParams, tz, start_ts: float, start_hour: int, fee: float,
                 slippage: float, continuous: bool = False):
        self.data = data
        self.engine = engine
        self.params = params
        self.strat = strat
        self.tz = tz
        self.start_ts = start_ts
        self.start_hour = start_hour
        self.fee = fee
        self.slip = slippage
        self.continuous = continuous
        self.idx = {s: {int(r[0]): i for i, r in enumerate(rows)} for s, rows in data.items()}
        self.c4 = {s: resample(rows, "1h", "4h", drop_incomplete=False) for s, rows in data.items()}
        self.c4ts = {s: [int(r[0]) for r in c] for s, c in self.c4.items()}

    def _closes_4h(self, s: str, bar_close_ms: int) -> list[float]:
        # 4h candles fully closed at bar_close_ms
        j = bisect.bisect_right(self.c4ts[s], bar_close_ms - 4 * H)
        return [r[4] for r in self.c4[s][max(0, j - (self.strat.ema_slow + 60)):j]]

    def run(self, name: str) -> Result:
        res = Result(name)
        timeline = sorted({int(r[0]) for rows in self.data.values() for r in rows if r[0] >= self.start_ts * 1000})
        st = None
        sid = 0
        daily_realized: dict[str, float] = {}
        daily_budget: dict[str, float] = {}
        open_meta: dict[str, float] = {}
        for ts in timeline:
            bar_close = (ts + H) / 1000.0
            local = datetime.fromtimestamp(ts / 1000, self.tz)
            day = local.strftime("%Y-%m-%d")
            bars = {s: self.data[s][self.idx[s][ts]] for s in self.data if ts in self.idx[s]}

            # --- start a session on schedule
            due = (sid == 0) if self.continuous else (local.hour == self.start_hour)
            if st is None and due:
                ok_daily = daily_budget.get(day, 0) + self.params.budget_usdt <= self.engine.daily_caps.daily_budget_cap_usdt
                ok_loss = daily_realized.get(day, 0) > -self.engine.daily_caps.daily_loss_cap_usdt
                if ok_daily and ok_loss:
                    sid += 1
                    st = self.engine.new_session(sid, self.params, ts / 1000.0)
                    daily_budget[day] = daily_budget.get(day, 0) + self.params.budget_usdt
            if st is None:
                continue

            def close(sym: str, price: float, reason: str) -> None:
                pos = st.positions[sym]
                gross = pos.qty * price
                fee = gross * self.fee
                pnl = self.engine.record_close(st, sym, gross - fee, fee)
                daily_realized[day] = daily_realized.get(day, 0) + pnl
                res.trades.append(TradeRow(st.session_id, sym, open_meta.pop(sym), bar_close,
                                           pos.entry_price, price, pnl, reason))

            # --- SL/TP intrabar (pessimistic: SL first if both touched)
            for sym, pos in list(st.positions.items()):
                b = bars.get(sym)
                if not b:
                    continue
                o, h, l = b[1], b[2], b[3]
                if l <= pos.stop_loss:
                    close(sym, min(o, pos.stop_loss) * (1 - self.slip), "SL")
                elif h >= pos.take_profit:
                    close(sym, max(o, pos.take_profit), "TP")

            # --- limits: worst case inside the bar (lows), then at the close
            for field_i in (3, 4):
                if st is None or st.status == SessionStatus.ENDED:
                    break
                prices = {s: b[field_i] for s, b in bars.items()}
                cur_realized_today = sum(t.pnl for t in res.trades
                                         if t.session_id == st.session_id and
                                         datetime.fromtimestamp(t.exit_ts, self.tz).strftime("%Y-%m-%d") == day)
                daily = DailyStats(daily_budget.get(day, 0), daily_realized.get(day, 0) - cur_realized_today)
                actions = self.engine.evaluate(st, prices, bar_close, daily)
                for a in actions:
                    if a.kind == "close_all":
                        for sym in list(st.positions):
                            close(sym, prices.get(sym, st.positions[sym].entry_price) * (1 - self.slip),
                                  f"session:{a.reason.value if a.reason else ''}")
                        self.engine.finish(st, a.reason, bar_close)
                if st.status == SessionStatus.ENDED:
                    break

            if st.status != SessionStatus.ENDED:
                # --- entries at bar close
                for sym, b in bars.items():
                    ok, _, size = self.engine.can_open(st, sym, bar_close)
                    if not ok:
                        continue
                    i = self.idx[sym][ts]
                    c1 = [r[4] for r in self.data[sym][max(0, i - 299):i + 1]]
                    sig = generate_signal(sym, c1, self._closes_4h(sym, ts + H), self.strat)
                    if sig is None:
                        continue
                    price = b[4] * (1 + self.slip)
                    qty = size / price * (1 - self.fee)
                    p = st.params
                    self.engine.record_open(st, Position(
                        symbol=sym, qty=qty, entry_price=price, cost_usdt=size, entry_fee_usdt=size * self.fee,
                        stop_loss=price * (1 - p.stop_loss_pct / 100), take_profit=price * (1 + p.take_profit_pct / 100),
                        opened_at=bar_close))
                    open_meta[sym] = bar_close

            if st.status == SessionStatus.ENDED:
                res.sessions.append(SessionResult(st.session_id, st.started_at,
                                                  st.stop_reason.value if st.stop_reason else "?",
                                                  st.realized_pnl, st.fees_paid, st.trades_closed,
                                                  st.wins, st.max_drawdown))
                res.equity += st.realized_pnl
                res.equity_peak = max(res.equity_peak, res.equity)
                res.equity_max_dd = max(res.equity_max_dd, res.equity_peak - res.equity)
                st = None
        if st is not None:  # close the tail at last prices
            for sym in list(st.positions):
                last = self.data[sym][-1][4]
                gross = st.positions[sym].qty * last
                self.engine.record_close(st, sym, gross * (1 - self.fee), gross * self.fee)
            res.sessions.append(SessionResult(st.session_id, st.started_at, "end_of_data", st.realized_pnl,
                                              st.fees_paid, st.trades_closed, st.wins, st.max_drawdown))
            res.equity += st.realized_pnl
        return res


def _reason_ru(value: str) -> str:
    try:
        return STOP_REASON_RU[StopReason(value)]
    except ValueError:
        return value


def summarize(r: Result, budget: float) -> str:
    n = len(r.trades)
    wins = sum(1 for t in r.trades if t.pnl > 0)
    pnl = sum(s.pnl for s in r.sessions)
    fees = sum(s.fees for s in r.sessions)
    reasons = Counter(s.reason for s in r.sessions)
    lines = [f"=== {r.name} ===",
             f"Сессий: {len(r.sessions)}, сделок: {n}, винрейт: {wins / n * 100 if n else 0:.1f}%",
             f"PnL (с комиссиями): {pnl:+.4f} USDT, комиссии: {fees:.4f} USDT",
             f"Макс. просадка (по завершённым сессиям): {r.equity_max_dd:.4f} USDT"]
    if r.sessions:
        best = max(r.sessions, key=lambda s: s.pnl)
        worst = min(r.sessions, key=lambda s: s.pnl)
        intra = max(s.max_dd for s in r.sessions)
        lines += [f"Средний PnL сессии: {pnl / len(r.sessions):+.4f} ({pnl / len(r.sessions) / budget * 100:+.2f}% бюджета)",
                  f"Лучшая: {best.pnl:+.4f}, худшая: {worst.pnl:+.4f}, макс. просадка внутри сессии: {intra:.4f}",
                  "Причины остановки: " + ", ".join(
                      f"{_reason_ru(k)}={v}" for k, v in reasons.most_common())]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("params", nargs="*", help="key=value как в /session_start")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--start-hour", type=int, default=None, help="час старта сессии (локальное время)")
    ap.add_argument("--fee", type=float, default=None)
    ap.add_argument("--slippage", type=float, default=0.0005)
    ap.add_argument("--compare", action="store_true", help="также прогнать без лимитов сессии")
    ap.add_argument("--refresh", action="store_true", help="перекачать историю")
    ap.add_argument("--trades-csv", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config, with_secrets=False)
    overrides = {}
    for kv in args.params:
        k, v = kv.split("=", 1)
        overrides[resolve_param(k)] = v
    params = cfg.session_defaults.replace(**overrides)
    engine = RiskEngine(cfg.hard_caps, cfg.daily_caps, args.fee if args.fee is not None else cfg.taker_fee)
    errs = engine.validate_params(params)
    if errs:
        print("Параметры нарушают hard_caps:\n- " + "\n- ".join(errs))
        return 2
    strat = StrategyParams.from_dict(cfg["trader"]["strategy"])
    symbols = [s.upper() for s in (args.symbols or cfg.symbols)]
    start_hour = args.start_hour
    if start_hour is None:
        start_hour = int(str(cfg["schedule"]["start_time"]).split(":")[0])
    print(f"Загрузка истории 1h за {args.days} дн. (+прогрев для EMA200 на 4h)...", file=sys.stderr)
    data = {s: load_history(s, args.days, refresh=args.refresh) for s in symbols}
    start_ts = time.time() - args.days * 86400
    fee = engine.fee_rate

    bt = Backtester(data, engine, params, strat, cfg.tz, start_ts, start_hour, fee, args.slippage)
    r = bt.run(f"С лимитами сессии (старт {start_hour:02d}:00, {params.duration_hours:g}ч, бюджет {params.budget_usdt:g})")
    print(summarize(r, params.budget_usdt))
    if args.trades_csv:
        with open(args.trades_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["session", "symbol", "entry_time", "exit_time", "entry", "exit", "pnl", "reason"])
            for t in r.trades:
                w.writerow([t.session_id, t.symbol, datetime.fromtimestamp(t.entry_ts, cfg.tz).isoformat(),
                            datetime.fromtimestamp(t.exit_ts, cfg.tz).isoformat(), t.entry, t.exit,
                            round(t.pnl, 6), t.reason])
    if args.compare:
        big = 1e12
        caps = HardCaps(max_budget_usdt=big, max_loss_usdt=big, max_duration_hours=big,
                        max_trades_per_session=10 ** 9, max_position_size_pct=params.position_size_pct,
                        max_open_positions=params.max_open_positions)
        free = params.replace(duration_hours=args.days * 24 + 48, max_loss_usdt=None, max_loss_pct=None,
                              profit_target_usdt=None, profit_target_pct=None, profit_lock=False,
                              max_trades=10 ** 8, max_consecutive_losses=0)
        e2 = RiskEngine(caps, DailyCaps(big, big), fee)
        bt2 = Backtester(data, e2, free, strat, cfg.tz, start_ts, start_hour, fee, args.slippage, continuous=True)
        r2 = bt2.run("Без лимитов сессии (только SL/TP, торговля 24/7)")
        print()
        print(summarize(r2, params.budget_usdt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
