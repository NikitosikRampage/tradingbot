"""5-minute scalping entries (scalp_revert / scalp_breakout) and the 5m backtester mode."""
import math
import time
from zoneinfo import ZoneInfo

import pytest

from backtest import MIN_FEE, Backtester, compute_metrics, lookahead_check
from bot.exits import ExitConfig
from bot.risk import DailyCaps, HardCaps, RiskEngine, SessionParams
from bot.strategy import StrategyParams, exit_levels, generate_scalp_signal

M5 = 300_000
UP_1H = [100 + i * 0.1 for i in range(260)]
DOWN_1H = [200 - i * 0.1 for i in range(260)]


def candles(closes, vols=None):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append([i * M5, prev, max(prev, c) * 1.0005, min(prev, c) * 0.9995, c, (vols or [10.0] * len(closes))[i]])
        prev = c
    return out


def wiggle(n, base=100.0):
    return [base * (1 + 0.001 * math.sin(i / 1.7)) for i in range(n)]


def test_scalp_modes_switch_timeframes():
    assert StrategyParams(entry_mode="scalp_revert").signal_tf == "5m"
    assert StrategyParams(entry_mode="scalp_breakout").trend_tf == "1h"
    assert StrategyParams().signal_tf == "1h" and StrategyParams().trend_tf == "4h"


def test_revert_fires_on_oversold_dip_in_uptrend_only():
    closes = wiggle(80) + [100 * (1 - 0.003 * k) for k in range(1, 9)]
    p = StrategyParams(entry_mode="scalp_revert")
    sig = generate_scalp_signal("ETH/USDT", candles(closes), UP_1H, p)
    assert sig is not None and sig.rsi_now < p.scalp_rsi_max and "Боллинджер" in sig.reason
    assert generate_scalp_signal("ETH/USDT", candles(closes), DOWN_1H, p) is None   # no 1h uptrend
    assert generate_scalp_signal("ETH/USDT", candles(wiggle(90)), UP_1H, p) is None  # no dip


def test_breakout_needs_volume():
    closes = wiggle(80) + [101.0]
    vols = [10.0] * 80 + [40.0]
    p = StrategyParams(entry_mode="scalp_breakout")
    assert generate_scalp_signal("ETH/USDT", candles(closes, vols), UP_1H, p) is not None
    assert generate_scalp_signal("ETH/USDT", candles(closes, [10.0] * 81), UP_1H, p) is None


def test_btc_regime_filter_for_scalp():
    closes = wiggle(80) + [101.0]
    vols = [10.0] * 80 + [40.0]
    p = StrategyParams(entry_mode="scalp_breakout", btc_regime=True)
    assert generate_scalp_signal("ETH/USDT", candles(closes, vols), UP_1H, p, btc_closes_1h=DOWN_1H) is None
    assert generate_scalp_signal("ETH/USDT", candles(closes, vols), UP_1H, p, btc_closes_1h=UP_1H) is not None


def test_scalp_target_covers_fees_and_stop_is_capped_by_session():
    closes = wiggle(80) + [101.0]
    vols = [10.0] * 80 + [40.0]
    p = StrategyParams(entry_mode="scalp_breakout", scalp_tp_atr=0.01, scalp_sl_atr=50)
    sig = generate_scalp_signal("ETH/USDT", candles(closes, vols), UP_1H, p, fee_rate=0.001)
    assert sig.tp_pct >= 3 * 2 * 0.1  # target never below 3× the round-trip fee
    sl, tp = exit_levels(100.0, sig, stop_loss_pct=2.0, take_profit_pct=4.0)
    assert sl == pytest.approx(98.0)  # the ATR stop cannot be wider than the session stop


def synthetic_5m(n=4200, seed=1.0):
    now = int(time.time() * 1000) // M5 * M5
    rows, p = [], 100.0
    for i in range(n):
        o = p
        c = p * (1.00004 + 0.003 * math.sin(i / 3.1 * seed) - 0.003 * math.sin((i - 1) / 3.1 * seed))
        vol = 100.0
        if i % 37 == 0:   # a volume burst with a jump: breakout material
            c, vol = c * 1.006, 300.0
        elif i % 53 == 0:  # a sharp dip: mean-reversion material
            c = c * 0.99
        rows.append([now - (n - i) * M5, o, max(o, c) * 1.001, min(o, c) * 0.999, c, vol])
        p = c
    return rows


def test_backtester_in_5m_mode_respects_limits_and_time_limit():
    data = {"BTC/USDT": synthetic_5m(), "ETH/USDT": synthetic_5m(seed=1.3)}
    eng = RiskEngine(HardCaps(), DailyCaps(30, 5), MIN_FEE)
    params = SessionParams(budget_usdt=10, duration_hours=24, max_loss_usdt=0.5, max_trades=20, no_entry_last_hours=1)
    sp = StrategyParams(entry_mode="scalp_breakout", scalp_vol_mult=1.5)
    bt = Backtester(data, eng, params, sp, ZoneInfo("UTC"), 0, MIN_FEE, 0.0005, ExitConfig(time_limit_hours=1))
    assert bt.bar_tf == "5m"
    r = bt.run("scalp", data["BTC/USDT"][0][0] / 1000)
    assert r.trades, "the synthetic breakouts must produce trades"
    for t in r.trades:
        assert t.exit_ts - t.entry_ts <= 3600 + 300 + 1
        assert t.fees >= 2 * MIN_FEE * 3.0 * 0.95
    for s in r.sessions:
        assert s.trades <= 20
    assert compute_metrics(r).trades == len(r.trades)


def test_no_lookahead_in_scalp_modes():
    data = {"BTC/USDT": synthetic_5m(3400), "ETH/USDT": synthetic_5m(3400, seed=1.3)}
    for mode in ("scalp_revert", "scalp_breakout"):
        sp = StrategyParams(entry_mode=mode, btc_regime=True)
        bt = Backtester(data, RiskEngine(HardCaps(), DailyCaps()), SessionParams(), sp, None, 0, MIN_FEE, 0)
        assert any(bt.signal_at("ETH/USDT", i) for i in range(2800, 3400)), "the check must see real signals"
        assert lookahead_check(data, sp, samples=200) == []
