"""Protections, exit barriers and the pairlist filters (all pure, no network)."""
import asyncio

import pytest

from bot.exits import ExitConfig, check_exit, raise_stop
from bot.pairlist import (PairlistConfig, PairMetrics, PairlistService, atr_pct, book_depth, evaluate_pair,
                          select_pairs)
from bot.protections import ClosedTrade, ProtectionConfig, compute_locks, entry_blocked
from bot.risk import Position

NOW = 1_800_000_000.0


def ct(sym, minutes_ago, pnl, kind="stop_loss", pct=None):
    return ClosedTrade(sym, NOW - minutes_ago * 60, pnl, pct if pct is not None else pnl / 3 * 100, kind)


# ---------------------------------------------------------------- protections
def test_disabled_protections_never_block():
    cfg = ProtectionConfig.from_dict({"enabled": False, "cooldown_minutes": 999})
    assert entry_blocked([ct("BTC/USDT", 1, -1)], cfg, "BTC/USDT", NOW) == (False, "")


def test_cooldown_per_pair_only():
    cfg = ProtectionConfig.from_dict({"enabled": True, "cooldown_minutes": 60})
    trades = [ct("BTC/USDT", 30, 0.1, "take_profit")]
    assert entry_blocked(trades, cfg, "BTC/USDT", NOW)[0]
    assert not entry_blocked(trades, cfg, "ETH/USDT", NOW)[0]
    assert not entry_blocked(trades, cfg, "BTC/USDT", NOW + 31 * 60)[0]


def test_stoploss_guard_locks_all_pairs():
    cfg = ProtectionConfig.from_dict({"enabled": True, "stoploss_guard": {
        "enabled": True, "lookback_hours": 6, "trade_limit": 2, "stop_minutes": 120}})
    trades = [ct("BTC/USDT", 100, -0.06), ct("ETH/USDT", 50, -0.06)]
    blocked, why = entry_blocked(trades, cfg, "SOL/USDT", NOW)
    assert blocked and "StoplossGuard" in why
    assert not entry_blocked(trades, cfg, "SOL/USDT", NOW + 71 * 60)[0]   # 120 min after the 2nd SL
    # profitable or non-stoploss exits don't count
    assert not entry_blocked([ct("BTC/USDT", 100, -0.06), ct("ETH/USDT", 50, 0.1, "take_profit")],
                             cfg, "SOL/USDT", NOW)[0]


def test_low_profit_pairs():
    cfg = ProtectionConfig.from_dict({"enabled": True, "low_profit_pairs": {
        "enabled": True, "lookback_hours": 48, "trade_limit": 2, "required_profit_pct": 0.0, "stop_minutes": 600}})
    trades = [ct("XRP/USDT", 300, -0.06, pct=-2.1), ct("XRP/USDT", 200, 0.03, "take_profit", pct=1.0)]
    assert entry_blocked(trades, cfg, "XRP/USDT", NOW)[0]
    assert not entry_blocked(trades, cfg, "BTC/USDT", NOW)[0]


def test_max_drawdown_equity():
    cfg = ProtectionConfig.from_dict({"enabled": True, "max_drawdown": {
        "enabled": True, "lookback_hours": 24, "trade_limit": 3, "max_drawdown_usdt": 0.5, "stop_minutes": 60}})
    trades = [ct("A/USDT", 300, 0.2, "take_profit"), ct("B/USDT", 200, -0.4), ct("C/USDT", 100, -0.2)]
    locks = compute_locks(trades, cfg, NOW)
    assert any(lk.symbol is None and "MaxDrawdown" in lk.reason for lk in locks) is False  # lock expired
    assert entry_blocked(trades, cfg, "D/USDT", NOW - 99 * 60 + 30)[0]


def test_unknown_protection_key_rejected():
    with pytest.raises(ValueError):
        ProtectionConfig.from_dict({"enabled": True, "stoploss_gaurd": {}})


# ---------------------------------------------------------------- exits
def pos(entry=100.0):
    return Position("BTC/USDT", 0.03, entry, 3.0, 0.003, entry * 0.98, entry * 1.04, NOW,
                    highest=entry, initial_stop=entry * 0.98)


def test_trailing_stop_only_moves_up():
    p = pos()
    cfg = ExitConfig(trailing_enabled=True, trailing_activation_pct=2, trailing_distance_pct=1.5)
    assert not raise_stop(p, 101.0, cfg, 0.001)           # not activated yet
    assert raise_stop(p, 103.0, cfg, 0.001)
    assert p.stop_loss == pytest.approx(103 * 0.985)
    assert not raise_stop(p, 102.0, cfg, 0.001)           # lower high -> unchanged
    assert check_exit(p, 101.0, NOW, cfg)[0] == "trailing_stop"


def test_breakeven_covers_fees():
    p = pos()
    cfg = ExitConfig(breakeven_enabled=True, breakeven_activation_pct=1.5)
    raise_stop(p, 101.6, cfg, 0.001)
    assert p.stop_loss > 100 * (1 + 2 * 0.001)


def test_stop_never_crosses_take_profit():
    p = pos()
    raise_stop(p, 200.0, ExitConfig(trailing_enabled=True, trailing_activation_pct=1, trailing_distance_pct=0.1), 0.001)
    assert p.stop_loss < p.take_profit


def test_time_limit_and_plain_sl_tp():
    p = pos()
    cfg = ExitConfig(time_limit_hours=4)
    assert check_exit(p, 100.0, NOW + 3 * 3600, cfg) is None
    assert check_exit(p, 100.0, NOW + 4 * 3600, cfg)[0] == "time_limit"
    assert check_exit(p, 97.9, NOW, ExitConfig())[0] == "stop_loss"
    assert check_exit(p, 104.1, NOW, ExitConfig())[0] == "take_profit"


# ---------------------------------------------------------------- pairlist
D = 86_400_000


def daily(n=60, price=10.0, rng=0.04, vol=50_000_000, wiggle=0.01):
    out = []
    for i in range(n):
        c = price * (1 + wiggle * ((-1) ** i))
        out.append([int(NOW * 1000) - (n - i) * D, price, c * (1 + rng / 2), c * (1 - rng / 2), c, vol / c])
    return out


def book(mid=10.0, spread=0.0005, depth_each=200_000):
    lvl = depth_each / 10 / mid
    bids = [[mid * (1 - spread / 2) * (1 - 0.0005 * i), lvl] for i in range(10)]
    asks = [[mid * (1 + spread / 2) * (1 + 0.0005 * i), lvl] for i in range(10)]
    return {"bids": bids, "asks": asks}


MARKET = {"info": {"listTime": str(int((NOW - 400 * 86400) * 1000))},
          "limits": {"amount": {"min": 0.01}, "cost": {"min": 0}}}


def ticker(last=10.0, qv=50_000_000, open_=9.9):
    return {"last": last, "quoteVolume": qv, "open": open_}


def ev(sym="ABC/USDT", **kw):
    args = dict(ticker=ticker(), market=MARKET, book=book(), daily=daily(), cfg=PairlistConfig())
    args.update(kw)
    return evaluate_pair(sym, args["ticker"], args["market"], args["book"], args["daily"], args["cfg"], NOW, 3.0)


def test_good_pair_passes_with_metrics():
    m = ev()
    assert m.passed, m.reasons
    assert 0 < m.spread_pct < 0.1 and m.bid_depth > 50_000 and 2 <= m.atr_pct <= 12
    assert m.age_days == pytest.approx(400, abs=1)


@pytest.mark.parametrize("kw,reason", [
    (dict(ticker=ticker(qv=5_000_000)), "объём"),
    (dict(book=book(spread=0.004)), "спред"),
    (dict(book=book(depth_each=10_000)), "глубина"),
    (dict(daily=daily(rng=0.005, wiggle=0.001)), "ATR"),
    (dict(daily=daily(rng=0.3)), "ATR"),
    (dict(market={**MARKET, "info": {"listTime": str(int((NOW - 20 * 86400) * 1000))}}), "листинг"),
    (dict(ticker=ticker(last=13.0, open_=10.0)), "памп"),
    (dict(ticker=ticker(qv=300_000_000)), "всплеск объёма"),
    (dict(market={**MARKET, "limits": {"amount": {"min": 1.0}, "cost": {"min": 0}}}), "мин. ордер"),
])
def test_filters_reject(kw, reason):
    m = ev(**kw)
    assert not m.passed and any(reason in r for r in m.reasons), m.reasons


def test_stablecoins_leveraged_and_blacklist():
    assert "стейблкоин" in ev("USDC/USDT").reasons
    assert any("плечевой" in r for r in ev("BTC3L/USDT").reasons)
    assert "blacklist" in ev("PEPE/USDT", cfg=PairlistConfig(blacklist=["PEPE/USDT"])).reasons
    assert "blacklist" in ev("DOGE/USDT", cfg=PairlistConfig.from_dict({"blacklist": ["*DOG*/usdt"]})).reasons


def test_atr_and_depth_helpers():
    assert atr_pct(daily(rng=0.04), 14) == pytest.approx(4.0, rel=0.2)
    b, a, s = book_depth(book(), 1.0)
    assert b == pytest.approx(200_000, rel=0.05) and s == pytest.approx(0.05, rel=0.01)


def test_select_whitelist_first_and_max_pairs():
    ms = [PairMetrics("A/USDT", quote_volume=9e7, passed=True), PairMetrics("B/USDT", quote_volume=8e7, passed=True),
          PairMetrics("C/USDT", quote_volume=7e7, passed=True), PairMetrics("D/USDT", quote_volume=1e9, passed=False),
          PairMetrics("E/USDT", quote_volume=1e6, passed=True)]
    cfg = PairlistConfig(max_pairs=3, whitelist=["E/USDT", "D/USDT"])
    assert select_pairs(ms, cfg) == ["E/USDT", "A/USDT", "B/USDT"]   # D whitelisted but failed filters


def test_pairlist_service_keeps_old_list_when_nothing_passes(tmp_path):
    from bot.db import Database

    class Sel:
        def __init__(self, ms):
            self.ms = ms

        async def evaluate(self):
            return self.ms

    msgs, changes = [], []

    async def on_change(p):
        changes.append(p)

    db = Database(tmp_path / "db")
    cfg = PairlistConfig(enabled=True, max_pairs=2)
    good = [PairMetrics("A/USDT", quote_volume=9e7, passed=True), PairMetrics("B/USDT", quote_volume=8e7, passed=True)]
    svc = PairlistService(cfg, db, Sel(good), msgs.append, ["BTC/USDT"], on_change)
    assert asyncio.run(svc.refresh()) == ["A/USDT", "B/USDT"] and changes == [["A/USDT", "B/USDT"]]
    svc.selector = Sel([PairMetrics("X/USDT", quote_volume=1, passed=False, reasons=["x"])])
    assert asyncio.run(svc.refresh()) == ["A/USDT", "B/USDT"]
    # persisted across restarts
    svc2 = PairlistService(cfg, db, Sel([]), msgs.append, ["BTC/USDT"])
    assert svc2.symbols() == ["A/USDT", "B/USDT"]
    assert "A/USDT" in svc2.text()
