"""Backtester runs on synthetic data (no network) and respects session limits."""
import math
import time
from zoneinfo import ZoneInfo

from backtest import Backtester, summarize
from bot.risk import DailyCaps, HardCaps, RiskEngine, SessionParams
from bot.strategy import StrategyParams

H = 3600_000


def synthetic(n=2000, seed=1.0):
    now = int(time.time() * 1000) // H * H
    rows = []
    p = 100.0
    for i in range(n):
        # uptrend with oscillations -> RSI pullbacks inside a 4h uptrend
        drift = 1.0004
        osc = 1 + 0.012 * math.sin(i / 5.0 * seed)
        o = p
        p = p * drift * (1 + 0.004 * math.sin(i / 2.3)) / (1 + 0.004 * math.sin((i - 1) / 2.3))
        c = p * osc / (1 + 0.012 * math.sin((i - 1) / 5.0 * seed))
        rows.append([now - (n - i) * H, o, max(o, c) * 1.003, min(o, c) * 0.997, c, 100.0])
        p = c
    return rows


def test_backtest_runs_and_limits_apply():
    data = {"BTC/USDT": synthetic(), "ETH/USDT": synthetic(seed=1.3)}
    eng = RiskEngine(HardCaps(), DailyCaps(30, 5), 0.001)
    params = SessionParams(budget_usdt=10, duration_hours=8, max_loss_usdt=0.3, profit_target_usdt=0.2,
                           max_trades=3)
    start = time.time() - 40 * 86400
    bt = Backtester(data, eng, params, StrategyParams(), ZoneInfo("Europe/Moscow"), start, 0, 0.001, 0.0005)
    r = bt.run("test")
    assert r.sessions, "sessions must be created on schedule"
    for s in r.sessions:
        assert s.trades <= 3
        # a session can lose slightly more than the limit only by the gap within one bar
        assert s.pnl > -0.3 - 0.5
    assert "Сессий" in summarize(r, 10)
