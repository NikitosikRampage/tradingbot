"""Integration tests of the Trader (sessions, fills, SL/TP, restore) against a fake exchange."""
import asyncio
import json
import time

import pytest

from bot.collector import MarketState
from bot.config import load_config
from bot.db import Database
from bot.exchange import Fill
from bot.health import Health
from bot.risk import SessionStatus, StopReason
from bot.trader import Trader

FEE = 0.001


class FakeExchange:
    mode = "DEMO"
    live = False
    can_trade = True

    def __init__(self, usdt=100.0):
        self.usdt = usdt
        self.base: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.orders: dict[str, Fill] = {}
        self.algos: dict[str, str] = {}
        self.sells: list[tuple[str, float, float, float]] = []
        self.fail_buy = False

    async def free_usdt(self):
        return self.usdt

    async def market_buy_cost(self, symbol, cost, cid):
        if self.fail_buy:
            raise RuntimeError("boom")
        p = self.prices[symbol]
        qty = cost / p
        fee_base = qty * FEE
        self.usdt -= cost
        self.base[symbol] = self.base.get(symbol, 0) + qty - fee_base
        f = Fill("o" + cid, cid, qty, p, cost, fee_base, 0.0, "closed", {})
        self.orders[cid] = f
        return f

    async def market_sell(self, symbol, qty, cid):
        p = self.prices[symbol]
        gross = qty * p
        fee = gross * FEE
        self.base[symbol] -= qty
        self.usdt += gross - fee
        self.sells.append((symbol, qty, gross, fee))
        return Fill("s" + cid, cid, qty, p, gross, 0.0, fee, "closed", {})

    async def place_oco(self, symbol, qty, tp, sl):
        aid = f"algo{len(self.algos)}"
        self.algos[aid] = "live"
        return aid

    async def cancel_algo(self, symbol, aid):
        if self.algos.get(aid) == "live":
            self.algos[aid] = "canceled"

    async def algo_state(self, aid):
        return self.algos.get(aid)

    async def base_free(self, symbol):
        return self.base.get(symbol, 0.0)

    async def base_total(self, symbol):
        return self.base.get(symbol, 0.0)

    async def sells_since(self, symbol, since_ms):
        rows = [s for s in self.sells if s[0] == symbol]
        return sum(r[1] for r in rows), sum(r[2] for r in rows), sum(r[3] for r in rows)

    async def find_order(self, symbol, cid):
        return self.orders.get(cid)

    async def fetch_tickers(self, symbols):
        return {s: {"last": self.prices[s]} for s in symbols}

    # simulate the exchange-side OCO firing
    def trigger_oco(self, symbol, aid):
        qty = self.base[symbol]
        p = self.prices[symbol]
        self.base[symbol] = 0
        self.sells.append((symbol, qty, qty * p, qty * p * FEE))
        self.usdt += qty * p * (1 - FEE)
        self.algos[aid] = "effective"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(tmp_path / "none.yaml", root=tmp_path, with_secrets=False)
    cfg.raw["trader"]["enabled"] = True
    db = Database(tmp_path / "bot.db")
    ex = FakeExchange()
    market = MarketState()
    msgs: list[str] = []
    health = Health(tmp_path / "hb.json", 300, msgs.append)

    def make_trader():
        return Trader(cfg, db, ex, market, health, msgs.append)

    return cfg, db, ex, market, msgs, make_trader


def set_price(market, ex, sym, price):
    ex.prices[sym] = price
    market.update(sym, {"last": price})


def run(coro):
    return asyncio.run(coro)


async def open_position(trader, ex, market, sym="BTC/USDT", price=100.0):
    """Bypass the strategy: emulate a signal by calling the fill registration directly."""
    st = trader.state
    ok, reason, size = trader.engine.can_open(st, sym, time.time())
    assert ok, reason
    set_price(market, ex, sym, price)
    fill = await ex.market_buy_cost(sym, size, f"cid{sym[:3]}")
    tid = trader.db.insert_trade(session_id=st.session_id, symbol=sym, mode="demo", status="pending",
                                 client_order_id=f"cid{sym[:3]}", entry_ts=time.time(), entry_reason="test")
    await trader._register_fill(st, tid, sym, fill, "test")
    return st.positions[sym]


def test_start_refused_when_balance_low(env):
    cfg, db, ex, market, msgs, make = env
    ex.usdt = 5
    t = make()
    msg = run(t.start_session({"budget_usdt": 10}))
    assert "не запущена" in msg and t.state is None


def test_start_refused_when_trader_disabled(env):
    cfg, db, ex, market, msgs, make = env
    cfg.raw["trader"]["enabled"] = False
    t = make()
    assert "trader выключен" in run(t.start_session({}))


def test_start_refused_over_hard_cap(env):
    cfg, db, ex, market, msgs, make = env
    t = make()
    msg = run(t.start_session({"budget_usdt": cfg.hard_caps.max_budget_usdt + 1}))
    assert t.state is None and "max_budget_usdt" in msg


def test_full_cycle_sl_and_report(env):
    cfg, db, ex, market, msgs, make = env
    t = make()

    async def scenario():
        await t.start_session({"budget_usdt": 10, "max_loss_usdt": 2})
        pos = await open_position(t, ex, market)
        assert pos.algo_id is not None and pos.cost_usdt == pytest.approx(3.0)
        set_price(market, ex, "BTC/USDT", 97.9)   # below SL (-2%)
        await t.tick()
        assert "BTC/USDT" not in t.state.positions
        tr = db.trades("status='closed'")[0]
        assert tr["pnl_usdt"] < 0 and tr["exit_fee_usdt"] > 0
        # realised pnl includes both fees
        assert t.state.realized_pnl == pytest.approx(tr["pnl_usdt"])
        assert t.state.fees_paid == pytest.approx(tr["entry_fee_usdt"] + tr["exit_fee_usdt"])
        await t.end_session()
        assert t.state is None

    run(scenario())
    assert any("стоп-лосс" in m for m in msgs)
    assert any("🏁 Сессия" in m and "ручная остановка" in m for m in msgs)


def test_loss_limit_closes_everything_on_tick(env):
    cfg, db, ex, market, msgs, make = env
    t = make()

    async def scenario():
        await t.start_session({"budget_usdt": 10, "max_loss_usdt": 0.1, "stop_loss_pct": 10})
        await open_position(t, ex, market, "BTC/USDT", 100)
        await open_position(t, ex, market, "ETH/USDT", 50)
        set_price(market, ex, "BTC/USDT", 98.5)   # -1.5% of 3 USDT ≈ -0.05 + fees
        set_price(market, ex, "ETH/USDT", 49.0)   # -2%
        await t.tick()

    run(scenario())
    assert t.state is None
    row = db.one("SELECT * FROM sessions")
    assert row["status"] == "ended" and row["stop_reason"] == StopReason.MAX_LOSS.value
    assert len(db.trades("status='closed'")) == 2


def test_stop_file_emergency(env, tmp_path):
    cfg, db, ex, market, msgs, make = env
    t = make()

    async def scenario():
        await t.start_session({})
        await open_position(t, ex, market)
        (tmp_path / "STOP").write_text("")
        await t.tick()

    run(scenario())
    assert t.state is None and not t.enabled
    assert db.one("SELECT stop_reason FROM sessions")["stop_reason"] == "emergency"
    assert "trader выключен" in run(t.start_session({}))


def test_set_param_respects_hard_caps(env):
    cfg, db, ex, market, msgs, make = env
    t = make()

    async def scenario():
        await t.start_session({})
        r1 = await t.set_param("max_loss", str(cfg.hard_caps.max_loss_usdt + 1))
        r2 = await t.set_param("hours", "4")
        r3 = await t.set_param("budget", str(cfg.hard_caps.max_budget_usdt * 2))
        return r1, r2, r3

    r1, r2, r3 = run(scenario())
    assert r1.startswith("❌") and r2.startswith("✅") and not r3.startswith("✅")
    assert t.state.ends_at == pytest.approx(t.state.started_at + 4 * 3600)


def test_restore_after_restart_with_oco_fired_while_down(env):
    cfg, db, ex, market, msgs, make = env
    t = make()

    async def first_run():
        await t.start_session({"budget_usdt": 10, "duration_hours": 8})
        pos = await open_position(t, ex, market)
        await open_position(t, ex, market, "ETH/USDT", 50)
        return pos

    pos = run(first_run())
    started = t.state.started_at
    sid = t.state.session_id
    # process dies; while it is down the BTC take-profit fires on the exchange
    set_price(market, ex, "BTC/USDT", 104.5)
    ex.trigger_oco("BTC/USDT", pos.algo_id)

    t2 = make()
    run(t2.restore())
    st = t2.state
    assert st is not None and st.session_id == sid
    assert st.started_at == started                       # timer is not reset
    assert "BTC/USDT" not in st.positions and "ETH/USDT" in st.positions
    assert st.realized_pnl > 0 and st.wins == 1
    assert any("Восстановлена сессия" in m for m in msgs)


def test_restore_resolves_pending_order(env):
    cfg, db, ex, market, msgs, make = env
    t = make()
    run(t.start_session({}))
    sid = t.state.session_id
    # crash right after the order was sent: trade row 'pending', state has no position
    set_price(market, ex, "SOL/USDT", 20.0)
    fill = run(ex.market_buy_cost("SOL/USDT", 3.0, "pendingcid"))
    db.insert_trade(session_id=sid, symbol="SOL/USDT", mode="demo", status="pending",
                    client_order_id="pendingcid", entry_ts=time.time(), entry_reason="signal")
    t2 = make()
    run(t2.restore())
    assert "SOL/USDT" in t2.state.positions
    assert t2.state.positions["SOL/USDT"].qty == pytest.approx(fill.filled_base - fill.fee_base)
    assert db.trades("client_order_id='pendingcid'")[0]["status"] == "open"


def test_restore_expired_session_ends_on_first_tick(env):
    cfg, db, ex, market, msgs, make = env
    t = make()
    run(t.start_session({"duration_hours": 1}))
    # pretend the PC slept for 2 hours: shift the persisted times into the past
    row = db.one("SELECT state_json FROM sessions")
    d = json.loads(row["state_json"])
    d["started_at"] -= 7200
    d["ends_at"] -= 7200
    db.save_session_state(d)
    t2 = make()
    run(t2.restore())
    run(t2.tick())
    assert t2.state is None
    assert db.one("SELECT stop_reason FROM sessions")["stop_reason"] == "time"
    # and no new session was started automatically
    assert db.one("SELECT COUNT(*) n FROM sessions")["n"] == 1


def test_daily_budget_cap_across_sessions(env):
    cfg, db, ex, market, msgs, make = env
    cfg.daily_caps.daily_budget_cap_usdt = 15
    t = make()
    t.engine = cfg.risk_engine()
    run(t.start_session({"budget_usdt": 10}))
    run(t.end_session())
    msg = run(t.start_session({"budget_usdt": 10}))
    assert t.state is None and "daily_budget_cap" in msg


def test_failed_buy_is_recorded(env):
    cfg, db, ex, market, msgs, make = env
    t = make()
    run(t.start_session({}))
    ex.fail_buy = True
    from bot.strategy import Signal
    import bot.trader as tr
    orig = tr.generate_signal
    tr.generate_signal = lambda *a, **k: Signal("BTC/USDT", 100, "test", 1, 1, 30, 36)
    try:
        set_price(market, ex, "BTC/USDT", 100)
        run(t.try_enter("BTC/USDT"))
    finally:
        tr.generate_signal = orig
    assert db.trades()[0]["status"] == "failed" and not t.state.positions
    assert t.state.status == SessionStatus.ACTIVE
