import asyncio
import math
import time

import pytest

from bot.alerts import AlertEngine
from bot.collector import Collector, MarketState
from bot.config import load_config
from bot.db import Database
from bot.health import Health
from bot.indicators import correlation, ema, resample, rsi
from bot.strategy import StrategyParams, generate_signal


def test_ema_sma_seed():
    vals = [1, 2, 3, 4, 5, 6]
    e = ema(vals, 3)
    assert e[:2] == [None, None] and e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(4 * 0.5 + 2 * 0.5)


def test_rsi_bounds():
    up = [float(i) for i in range(30)]
    assert rsi(up, 14)[-1] == pytest.approx(100.0)
    down = [float(30 - i) for i in range(30)]
    assert rsi(down, 14)[-1] == pytest.approx(0.0)


def test_resample_4h_utc_aligned():
    h = 3600_000
    rows = [[i * h, 1, 2 + i, 0.5, 1 + i, 10] for i in range(9)]  # 00:00 .. 08:00
    out = resample(rows, "1h", "4h")
    assert [r[0] for r in out] == [0, 4 * h]  # last incomplete bucket (08:00) dropped
    assert out[0][2] == 5 and out[0][4] == 4 and out[0][5] == 40


def test_correlation():
    a = [math.sin(i / 3) for i in range(50)]
    assert correlation(a, a) == pytest.approx(1.0)
    assert correlation(a, [-x for x in a]) == pytest.approx(-1.0)


def test_signal_requires_uptrend_and_rsi_cross():
    p = StrategyParams(ema_fast=5, ema_slow=20, rsi_period=5, rsi_pullback=35, rsi_max_entry=60)
    up4h = [100 + i for i in range(40)]
    # 1h: rally, sharp dip (RSI < 35) then one up candle (RSI crosses 35)
    c1h = [100 + i * 0.5 for i in range(30)] + [113, 112, 111, 110, 111.2]
    sig = generate_signal("BTC/USDT", c1h, up4h, p)
    assert sig is not None and "RSI" in sig.reason
    down4h = list(reversed(up4h))
    assert generate_signal("BTC/USDT", c1h, down4h, p) is None


@pytest.fixture
def stack(tmp_path):
    cfg = load_config(tmp_path / "x.yaml", root=tmp_path, with_secrets=False)
    db = Database(tmp_path / "db.sqlite")
    market = MarketState()
    msgs = []
    health = Health(tmp_path / "hb.json", 300, msgs.append)
    col = Collector(cfg, db, None, market, health)
    alerts = AlertEngine(cfg, db, market, msgs.append)
    col.on_tick.append(alerts.on_tick)
    return cfg, db, market, col, alerts, msgs


def test_collector_parses_ws_messages(stack):
    cfg, db, market, col, alerts, msgs = stack
    closed = []
    col.on_candle_closed.append(lambda s, tf, r: closed.append((s, tf, r[0])))

    async def go():
        await col._handle({"arg": {"channel": "tickers", "instId": "BTC-USDT"},
                           "data": [{"instId": "BTC-USDT", "last": "65000.1", "bidPx": "65000",
                                     "askPx": "65000.2", "high24h": "66000", "low24h": "64000",
                                     "open24h": "64500", "volCcy24h": "1", "ts": "1"}]})
        await col._handle({"arg": {"channel": "candle1m", "instId": "ETH-USDT"},
                           "data": [["60000", "1", "2", "0.5", "1.5", "10", "0", "0", "0"]]})
        # next candle starts without confirm of the previous -> previous is closed anyway
        await col._handle({"arg": {"channel": "candle1m", "instId": "ETH-USDT"},
                           "data": [["120000", "1.5", "2", "1", "1.8", "5", "0", "0", "0"]]})
        await col._handle({"arg": {"channel": "candle1m", "instId": "ETH-USDT"},
                           "data": [["120000", "1.5", "2", "1", "1.9", "7", "0", "0", "1"]]})

    asyncio.run(go())
    assert market.price("BTC/USDT") == pytest.approx(65000.1)
    assert market.fresh("BTC/USDT", 60)
    assert closed == [("ETH/USDT", "1m", 60000), ("ETH/USDT", "1m", 120000)]
    assert db.last_candle_ts("ETH/USDT", "1m") == 120000


def test_price_move_alert_with_cooldown(stack):
    cfg, db, market, col, alerts, msgs = stack
    now_ms = int(time.time() * 1000)
    ref = (now_ms - 15 * 60_000) // 60_000 * 60_000
    db.upsert_candles("BTC/USDT", "1m", [[ref - 60_000, 90, 90, 90, 90, 1], [ref, 100, 100, 100, 100, 1],
                                         [ref + 60_000, 101, 101, 101, 101, 1], [now_ms // 60_000 * 60_000, 102, 103, 102, 103, 1]])
    alerts.check_price_move("BTC/USDT", 103)
    alerts.check_price_move("BTC/USDT", 104)  # within cooldown -> suppressed
    assert len([m for m in msgs if "BTC/USDT +3.00%" in m]) == 1
    assert len(msgs) == 1


def test_volume_spike_and_levels(stack):
    cfg, db, market, col, alerts, msgs = stack
    rows = [[i * 300_000, 1, 1, 1, 1, 10] for i in range(20)] + [[20 * 300_000, 1, 1.1, 1, 1.1, 50]]
    db.upsert_candles("SOL/USDT", "5m", rows)
    alerts.check_volume("SOL/USDT", "5m")
    assert any("всплеск объёма" in m for m in msgs)
    h = 3600_000
    db.upsert_candles("XRP/USDT", "1h", [[i * h, 1, 1.2, 0.8, 1, 1] for i in range(30)])
    alerts.check_levels("XRP/USDT", 1.25)
    assert any("пробил 24h high" in m for m in msgs)
    # bounce: touch the low, then rise by > bounce_pct
    alerts.check_levels("XRP/USDT", 0.801)
    alerts.check_levels("XRP/USDT", 0.82)
    assert any("отскок вверх от 24h low" in m for m in msgs)


# ---------------------------------------------------------------- new strategy modes
def _rows(closes, spread=0.004):
    return [[i * 3_600_000, c, c * (1 + spread), c * (1 - spread), c, 1.0] for i, c in enumerate(closes)]


def test_breakout_entry_and_atr_exits():
    from bot.strategy import exit_levels
    p = StrategyParams(ema_fast=5, ema_slow=20, rsi_period=5, entry_mode="breakout", breakout_bars=10,
                       rsi_max_breakout=101, exit_mode="atr", atr_period=5, atr_sl_mult=2, atr_tp_mult=3)
    up4h = [100 + i for i in range(40)]
    c1h = [100.0] * 30 + [100.2, 100.1, 100.3, 100.2, 101.5]
    sig = generate_signal("ETH/USDT", c1h, up4h, p, candles_1h=_rows(c1h))
    assert sig is not None and "пробой" in sig.reason and sig.atr_pct > 0
    assert sig.sl_pct == pytest.approx(2 * sig.atr_pct) and sig.tp_pct == pytest.approx(3 * sig.atr_pct)
    # the stop is never wider than the session stop_loss_pct
    sl, tp = exit_levels(100.0, sig, stop_loss_pct=0.1, take_profit_pct=4)
    assert sl == pytest.approx(99.9) and tp == pytest.approx(100 * (1 + sig.tp_pct / 100))
    # no breakout -> no signal
    assert generate_signal("ETH/USDT", c1h[:-1] + [100.25], up4h, p, candles_1h=_rows(c1h[:-1] + [100.25])) is None


def test_min_atr_filter_and_btc_regime():
    p = StrategyParams(ema_fast=5, ema_slow=20, rsi_period=5, min_atr_pct=5.0)
    up4h = [100 + i for i in range(40)]
    c1h = [100 + i * 0.5 for i in range(30)] + [113, 112, 111, 110, 111.2]
    assert generate_signal("SOL/USDT", c1h, up4h, p, candles_1h=_rows(c1h)) is None      # ATR too small
    p2 = StrategyParams(ema_fast=5, ema_slow=20, rsi_period=5, btc_regime=True)
    down = list(reversed(up4h))
    assert generate_signal("SOL/USDT", c1h, up4h, p2, candles_1h=_rows(c1h), btc_closes_4h=down) is None
    assert generate_signal("SOL/USDT", c1h, up4h, p2, candles_1h=_rows(c1h), btc_closes_4h=up4h) is not None
    assert generate_signal("BTC/USDT", c1h, up4h, p2, candles_1h=_rows(c1h), btc_closes_4h=down) is not None


def test_default_params_keep_original_behaviour():
    p = StrategyParams(ema_fast=5, ema_slow=20, rsi_period=5)
    up4h = [100 + i for i in range(40)]
    c1h = [100 + i * 0.5 for i in range(30)] + [113, 112, 111, 110, 111.2]
    sig = generate_signal("BTC/USDT", c1h, up4h, p)          # no candles needed in the default mode
    assert sig is not None and sig.sl_pct is None and sig.tp_pct is None


def test_unknown_strategy_param_rejected():
    with pytest.raises(ValueError):
        StrategyParams.from_dict({"entry_mode": "magic"})
    with pytest.raises(ValueError):
        StrategyParams.from_dict({"rsi_pulback": 30})
