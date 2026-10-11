"""Backtester on synthetic data (no network): limits, exits, protections, lookahead, Monte Carlo."""
import math
import time
from zoneinfo import ZoneInfo

from backtest import (MIN_FEE, Backtester, compute_metrics, lookahead_check, monte_carlo, per_pair_table,
                      summarize)
from bot.exits import ExitConfig
from bot.protections import ProtectionConfig
from bot.risk import DailyCaps, HardCaps, RiskEngine, SessionParams
from bot.strategy import StrategyParams

H = 3600_000
TZ = ZoneInfo("Europe/Moscow")


def synthetic(n=2000, seed=1.0):
    now = int(time.time() * 1000) // H * H
    rows = []
    p = 100.0
    for i in range(n):
        o = p
        p = p * 1.0004 * (1 + 0.004 * math.sin(i / 2.3)) / (1 + 0.004 * math.sin((i - 1) / 2.3))
        c = p * (1 + 0.012 * math.sin(i / 5.0 * seed)) / (1 + 0.012 * math.sin((i - 1) / 5.0 * seed))
        rows.append([now - (n - i) * H, o, max(o, c) * 1.003, min(o, c) * 0.997, c, 100.0])
        p = c
    return rows


def make_bt(params=None, exits=None, prot=None, data=None):
    data = data or {"BTC/USDT": synthetic(), "ETH/USDT": synthetic(seed=1.3)}
    eng = RiskEngine(HardCaps(), DailyCaps(30, 5), MIN_FEE)
    params = params or SessionParams(budget_usdt=10, duration_hours=8, max_loss_usdt=0.3,
                                     profit_target_usdt=0.2, max_trades=3)
    return Backtester(data, eng, params, StrategyParams(), TZ, 0, MIN_FEE, 0.0005, exits, prot)


START = time.time() - 40 * 86400


def test_backtest_runs_and_limits_apply():
    r = make_bt().run("test", START)
    assert r.sessions, "sessions must be created on schedule"
    for s in r.sessions:
        assert s.trades <= 3
        assert s.pnl > -0.3 - 0.5  # only the gap within one bar can exceed the limit
    m = compute_metrics(r)
    assert m.trades == len(r.trades) and m.fees > 0
    assert "Сессий" in summarize(r, 10, mc_runs=200)
    assert "t-stat" in per_pair_table(r)


def test_costs_are_charged_on_every_trade():
    r = make_bt().run("test", START)
    for t in r.trades:
        # entry + exit fee >= 0.1% of the position each side
        assert t.fees >= 2 * MIN_FEE * 3.0 * 0.95


def test_exits_and_protections_only_restrict():
    base = make_bt(params=SessionParams(budget_usdt=10, duration_hours=24, max_loss_usdt=5, max_trades=50,
                                        max_consecutive_losses=0)).run("b", START)
    prot = ProtectionConfig.from_dict({"enabled": True, "cooldown_minutes": 600})
    guarded = make_bt(params=SessionParams(budget_usdt=10, duration_hours=24, max_loss_usdt=5, max_trades=50,
                                           max_consecutive_losses=0), prot=prot).run("p", START)
    assert len(guarded.trades) <= len(base.trades)
    tl = make_bt(exits=ExitConfig(time_limit_hours=2)).run("t", START)
    assert all(t.kind in ("stop_loss", "take_profit", "time_limit", "session", "trailing_stop") for t in tl.trades)
    assert all(t.exit_ts - t.entry_ts <= 3 * 3600 + 1 for t in tl.trades if t.kind == "time_limit")


def test_walk_forward_windows_are_independent():
    bt = make_bt()
    now = time.time()
    a = bt.run("w1", START, START + 20 * 86400)
    b = bt.run("w2", START + 20 * 86400, now)
    assert all(t.entry_ts < START + 20 * 86400 + 3600 for t in a.trades)
    assert all(t.entry_ts >= START + 20 * 86400 for t in b.trades)


def test_no_lookahead_bias():
    data = {"BTC/USDT": synthetic(1400)}
    assert lookahead_check(data, StrategyParams(), samples=60) == []


def test_monte_carlo_distribution():
    mc = monte_carlo([0.1, -0.05, 0.08, -0.06, 0.02] * 10, runs=500)
    assert mc["pnl_p5"] <= mc["pnl_p50"] <= mc["pnl_p95"]
    assert 0 <= mc["prob_positive"] <= 1


def test_lookahead_check_detects_a_biased_strategy():
    import bisect

    class Leaky(Backtester):
        def closes_4h(self, s, bar_close_ms):  # BUG on purpose: includes the still-forming 4h candle
            j = bisect.bisect_right(self.c4ts[s], bar_close_ms)
            return [r[4] for r in self.c4[s][max(0, j - 260):j]]

    data = {"BTC/USDT": synthetic(1400)}
    sp = StrategyParams(rsi_pullback=45, rsi_max_entry=70)
    assert lookahead_check(data, sp, samples=400, cls=Leaky) != []


def test_no_lookahead_in_breakout_atr_mode():
    sp = StrategyParams(entry_mode="breakout", exit_mode="atr", min_atr_pct=0.1, btc_regime=True)
    data = {"BTC/USDT": synthetic(1400), "ETH/USDT": synthetic(1400, seed=1.3)}
    assert lookahead_check(data, sp, samples=80) == []


def test_signal_cache_gives_identical_results():
    cache = {}
    a = make_bt().run("a", START)
    bt = make_bt()
    bt.signal_cache = cache
    b1 = bt.run("b1", START)
    b2 = bt.run("b2", START)       # second run served from the cache
    assert cache and [t.pnl for t in a.trades] == [t.pnl for t in b1.trades] == [t.pnl for t in b2.trades]


def test_random_entry_control_runs():
    bt = make_bt()
    bt.random_entry = (0.05, 7)
    r = bt.run("rnd", START)
    assert r.trades and all(t.kind for t in r.trades)
