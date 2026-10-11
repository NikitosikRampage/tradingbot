"""Tests for the session / risk engine."""
import json

import pytest

from bot.risk import (DailyCaps, DailyStats, HardCaps, Position, RiskEngine,
                      SessionParams, SessionState, SessionStatus, StopReason)

FEE = 0.001
T0 = 1_700_000_000.0


def engine(**caps):
    return RiskEngine(HardCaps(**caps), DailyCaps(daily_budget_cap_usdt=30, daily_loss_cap_usdt=5), FEE)


def params(**kw):
    base = dict(budget_usdt=10, duration_hours=8, max_loss_usdt=2, profit_target_usdt=1.5,
                max_trades=10, max_consecutive_losses=3)
    base.update(kw)
    return SessionParams(**base)


def open_pos(eng, st, symbol="BTC/USDT", price=100.0, cost=3.0):
    qty = cost / price * (1 - FEE)
    pos = Position(symbol=symbol, qty=qty, entry_price=price, cost_usdt=cost,
                   entry_fee_usdt=cost * FEE, stop_loss=price * 0.98, take_profit=price * 1.04,
                   opened_at=T0)
    eng.record_open(st, pos)
    return pos


def price_for_unrealized(pos, target_pnl):
    """Price at which unrealised pnl (net of exit fee) equals target_pnl."""
    return (target_pnl + pos.cost_usdt) / (pos.qty * (1 - FEE))


def kinds(actions):
    return [a.kind for a in actions]


# ------------------------------------------------------------------ hard caps
class TestHardCaps:
    def test_budget_over_cap_rejected(self):
        eng = engine(max_budget_usdt=50)
        errs = eng.validate_params(params(budget_usdt=51))
        assert any("max_budget_usdt" in e for e in errs)

    def test_loss_over_cap_rejected(self):
        eng = engine(max_loss_usdt=5)
        assert any("max_loss_usdt" in e for e in eng.validate_params(params(max_loss_usdt=6)))

    def test_duration_over_cap_rejected(self):
        eng = engine(max_duration_hours=12)
        assert any("max_duration_hours" in e for e in eng.validate_params(params(duration_hours=13)))

    def test_position_size_and_positions_capped(self):
        eng = engine()
        assert eng.validate_params(params(position_size_pct=31))
        assert eng.validate_params(params(max_open_positions=3))

    def test_valid_params_ok(self):
        assert engine().validate_params(params()) == []

    def test_update_param_cannot_exceed_caps(self):
        eng = engine(max_budget_usdt=20, max_loss_usdt=3)
        st = eng.new_session(1, params(), T0)
        with pytest.raises(ValueError):
            eng.update_param(st, "budget_usdt", "25")
        with pytest.raises(ValueError):
            eng.update_param(st, "max_loss_usdt", "4")
        assert st.params.budget_usdt == 10  # unchanged
        eng.update_param(st, "budget_usdt", "15")
        assert st.params.budget_usdt == 15

    def test_effective_max_loss_never_above_cap(self):
        eng = engine(max_loss_usdt=3)
        # no session-level limit at all -> hard cap still applies
        p = params(max_loss_usdt=None, max_loss_pct=None)
        assert eng.effective_max_loss(p) == 3
        # pct-based limit is the tightest
        p = params(max_loss_usdt=2.5, max_loss_pct=10)  # 10% of 10 = 1
        assert eng.effective_max_loss(p) == pytest.approx(1.0)

    def test_new_session_rejects_invalid(self):
        with pytest.raises(ValueError):
            engine(max_budget_usdt=5).new_session(1, params(budget_usdt=10), T0)


# ------------------------------------------------------------------ start checks
class TestStartChecks:
    def test_insufficient_balance(self):
        errs = engine().check_start(params(budget_usdt=10), free_usdt=9.5, daily=DailyStats(), has_active=False)
        assert any("баланс" in e.lower() for e in errs)

    def test_balance_ok(self):
        assert engine().check_start(params(), free_usdt=100, daily=DailyStats(), has_active=False) == []

    def test_daily_budget_cap(self):
        errs = engine().check_start(params(budget_usdt=10), 100, DailyStats(budget_allocated_usdt=25), False)
        assert any("daily_budget_cap" in e for e in errs)

    def test_active_session_blocks(self):
        assert engine().check_start(params(), 100, DailyStats(), has_active=True)

    def test_daily_loss_already_hit(self):
        errs = engine().check_start(params(), 100, DailyStats(realized_pnl_other_usdt=-5), False)
        assert any("лимит убытка" in e for e in errs)


# ------------------------------------------------------------------ loss limit
class TestLossLimit:
    def test_unrealized_loss_triggers_close_all_on_tick(self):
        eng = engine()
        st = eng.new_session(1, params(max_loss_usdt=0.5), T0)
        pos = open_pos(eng, st, cost=3.0)
        # small loss -> nothing
        assert eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, -0.3)}, T0 + 60) == []
        # loss beyond the limit (no trade closed yet) -> close all
        acts = eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, -0.51)}, T0 + 120)
        assert kinds(acts) == ["close_all"]
        assert acts[0].reason == StopReason.MAX_LOSS
        assert st.status == SessionStatus.CLOSING
        assert eng.can_open(st, "ETH/USDT", T0 + 121)[0] is False

    def test_realized_plus_unrealized(self):
        eng = engine()
        st = eng.new_session(1, params(max_loss_usdt=1.0), T0)
        p1 = open_pos(eng, st, "ETH/USDT", 100, 3.0)
        eng.record_close(st, "ETH/USDT", proceeds_net=p1.cost_usdt - 0.6, exit_fee_usdt=0.003)
        assert st.realized_pnl == pytest.approx(-0.6)
        p2 = open_pos(eng, st, "BTC/USDT", 100, 3.0)
        # -0.6 realised + -0.3 unrealised = -0.9 -> ok
        assert eng.evaluate(st, {"BTC/USDT": price_for_unrealized(p2, -0.3)}, T0 + 10) == []
        # -0.6 + -0.45 = -1.05 -> stop
        acts = eng.evaluate(st, {"BTC/USDT": price_for_unrealized(p2, -0.45)}, T0 + 20)
        assert acts[0].reason == StopReason.MAX_LOSS

    def test_fees_count_in_pnl(self):
        eng = engine()
        st = eng.new_session(1, params(), T0)
        pos = open_pos(eng, st, cost=3.0)
        snap = eng.pnl(st, {"BTC/USDT": 100.0})  # price unchanged
        # entry fee (in qty) + estimated exit fee -> negative
        assert snap.total == pytest.approx(-(3.0 * FEE) - pos.qty * 100 * FEE)
        assert snap.total < 0

    def test_close_completes_session(self):
        eng = engine()
        st = eng.new_session(1, params(max_loss_usdt=0.2), T0)
        pos = open_pos(eng, st)
        eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, -0.3)}, T0 + 5)
        eng.record_close(st, "BTC/USDT", pos.cost_usdt - 0.3, 0.003)
        acts = eng.evaluate(st, {}, T0 + 6)
        assert kinds(acts) == ["end"]
        assert st.status == SessionStatus.ENDED
        assert st.stop_reason == StopReason.MAX_LOSS

    def test_daily_loss_cap(self):
        eng = engine()
        st = eng.new_session(1, params(max_loss_usdt=2), T0)
        pos = open_pos(eng, st)
        daily = DailyStats(realized_pnl_other_usdt=-4.5)
        acts = eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, -0.6)}, T0 + 5, daily)
        assert acts[0].reason == StopReason.DAILY_LOSS_CAP


# ------------------------------------------------------------------ profit target
class TestProfitTarget:
    def test_target_stops_and_locks_profit(self):
        eng = engine()
        st = eng.new_session(1, params(profit_target_usdt=0.5), T0)
        pos = open_pos(eng, st)
        acts = eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, 0.55)}, T0 + 5)
        assert kinds(acts) == ["close_all"]
        assert acts[0].reason == StopReason.PROFIT_TARGET

    def test_target_pct(self):
        eng = engine()
        st = eng.new_session(1, params(profit_target_usdt=None, profit_target_pct=5), T0)  # 0.5 USDT
        pos = open_pos(eng, st)
        assert eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, 0.45)}, T0 + 5) == []
        acts = eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, 0.51)}, T0 + 6)
        assert acts[0].reason == StopReason.PROFIT_TARGET

    def test_profit_lock_trailing(self):
        eng = engine()
        st = eng.new_session(1, params(profit_target_usdt=0.5, profit_lock=True,
                                       profit_lock_trail_pct=30), T0)
        pos = open_pos(eng, st)
        sym = "BTC/USDT"
        acts = eng.evaluate(st, {sym: price_for_unrealized(pos, 0.6)}, T0 + 1)
        assert kinds(acts) == ["notify"] and st.profit_lock_active
        assert st.status == SessionStatus.ACTIVE
        # keeps running, new peak 1.0
        assert eng.evaluate(st, {sym: price_for_unrealized(pos, 1.0)}, T0 + 2) == []
        assert st.peak_pnl == pytest.approx(1.0)
        # drawdown to 0.75 (25% from peak) -> still running
        assert eng.evaluate(st, {sym: price_for_unrealized(pos, 0.75)}, T0 + 3) == []
        # drawdown to 0.69 (>30% from peak) -> stop
        acts = eng.evaluate(st, {sym: price_for_unrealized(pos, 0.69)}, T0 + 4)
        assert kinds(acts) == ["close_all"] and acts[0].reason == StopReason.PROFIT_TRAIL


# ------------------------------------------------------------------ timer
class TestTimer:
    def test_close_all_at_end_of_time(self):
        eng = engine()
        st = eng.new_session(1, params(duration_hours=2, on_session_end="close_all"), T0)
        open_pos(eng, st)
        prices = {"BTC/USDT": 100.0}
        assert eng.evaluate(st, prices, T0 + 2 * 3600 - 1) == []
        acts = eng.evaluate(st, prices, T0 + 2 * 3600)
        assert kinds(acts) == ["close_all"] and acts[0].reason == StopReason.TIME

    def test_no_new_entries_after_time(self):
        eng = engine()
        st = eng.new_session(1, params(duration_hours=1), T0)
        assert eng.can_open(st, "BTC/USDT", T0 + 10)[0]
        assert not eng.can_open(st, "BTC/USDT", T0 + 3600)[0]

    def test_manage_only_waits_for_sl_tp_then_deadline(self):
        eng = engine()
        st = eng.new_session(1, params(duration_hours=1, on_session_end="manage_only",
                                       manage_only_max_minutes=30), T0)
        open_pos(eng, st)
        prices = {"BTC/USDT": 100.0}
        acts = eng.evaluate(st, prices, T0 + 3600)
        assert kinds(acts) == ["notify"]
        assert st.status == SessionStatus.WINDING_DOWN
        assert not eng.can_open(st, "ETH/USDT", T0 + 3601)[0]
        assert eng.evaluate(st, prices, T0 + 3600 + 29 * 60) == []
        acts = eng.evaluate(st, prices, T0 + 3600 + 30 * 60)
        assert kinds(acts) == ["close_all"] and acts[0].reason == StopReason.TIME

    def test_manage_only_ends_when_positions_closed(self):
        eng = engine()
        st = eng.new_session(1, params(duration_hours=1, on_session_end="manage_only"), T0)
        pos = open_pos(eng, st)
        eng.evaluate(st, {"BTC/USDT": 100.0}, T0 + 3600)
        eng.record_close(st, "BTC/USDT", pos.cost_usdt * 1.04, 0.003)
        acts = eng.evaluate(st, {}, T0 + 3700)
        assert kinds(acts) == ["end"] and st.status == SessionStatus.ENDED
        assert st.stop_reason == StopReason.TIME

    def test_end_without_positions(self):
        eng = engine()
        st = eng.new_session(1, params(duration_hours=1), T0)
        acts = eng.evaluate(st, {}, T0 + 3600)
        assert kinds(acts) == ["end"] and st.status == SessionStatus.ENDED

    def test_duration_change_moves_deadline(self):
        eng = engine(max_duration_hours=10)
        st = eng.new_session(1, params(duration_hours=2), T0)
        eng.update_param(st, "duration_hours", "4")
        assert st.ends_at == T0 + 4 * 3600
        with pytest.raises(ValueError):
            eng.update_param(st, "duration_hours", "11")


# ------------------------------------------------------------------ limits on entries
class TestEntries:
    def test_position_size_le_30pct_and_budget(self):
        eng = engine()
        st = eng.new_session(1, params(budget_usdt=10, position_size_pct=30), T0)
        ok, _, size = eng.can_open(st, "BTC/USDT", T0)
        assert ok and size == pytest.approx(3.0)

    def test_max_two_positions_and_one_per_coin(self):
        eng = engine()
        st = eng.new_session(1, params(), T0)
        open_pos(eng, st, "BTC/USDT")
        assert not eng.can_open(st, "BTC/USDT", T0)[0]
        open_pos(eng, st, "ETH/USDT")
        ok, reason, _ = eng.can_open(st, "SOL/USDT", T0)
        assert not ok and "одновременных" in reason

    def test_budget_never_exceeded_after_losses(self):
        eng = engine()
        st = eng.new_session(1, params(budget_usdt=10, position_size_pct=30, max_loss_usdt=5), T0)
        st.realized_pnl = -8.5   # (artificial) capital left: 1.5
        ok, _, size = eng.can_open(st, "BTC/USDT", T0)
        assert ok and size == pytest.approx(1.5)
        st.realized_pnl = -9.5
        assert not eng.can_open(st, "BTC/USDT", T0)[0]

    def test_profit_not_reinvested(self):
        eng = engine()
        st = eng.new_session(1, params(budget_usdt=10, position_size_pct=30), T0)
        st.realized_pnl = 5
        assert eng.can_open(st, "BTC/USDT", T0)[2] == pytest.approx(3.0)

    def test_stale_data_blocks_entries(self):
        eng = engine()
        st = eng.new_session(1, params(), T0)
        ok, reason, _ = eng.can_open(st, "BTC/USDT", T0, data_fresh=False)
        assert not ok and "устарели" in reason

    def test_max_trades(self):
        eng = engine()
        st = eng.new_session(1, params(max_trades=1), T0)
        pos = open_pos(eng, st)
        assert not eng.can_open(st, "ETH/USDT", T0)[0]
        assert eng.evaluate(st, {"BTC/USDT": 100.0}, T0 + 1) == []  # waits for open position
        eng.record_close(st, "BTC/USDT", pos.cost_usdt + 0.1, 0.003)
        acts = eng.evaluate(st, {}, T0 + 2)
        assert acts[0].reason == StopReason.MAX_TRADES and st.status == SessionStatus.ENDED

    def test_consecutive_losses_pause(self):
        eng = engine()
        st = eng.new_session(1, params(max_consecutive_losses=2, consecutive_loss_action="pause",
                                       consecutive_loss_pause_minutes=30, max_loss_usdt=5), T0)
        for sym in ("BTC/USDT", "ETH/USDT"):
            pos = open_pos(eng, st, sym)
            eng.record_close(st, sym, pos.cost_usdt - 0.05, 0.003)
        acts = eng.evaluate(st, {}, T0 + 10)
        assert kinds(acts) == ["notify"]
        assert not eng.can_open(st, "SOL/USDT", T0 + 11)[0]
        acts = eng.evaluate(st, {}, T0 + 10 + 30 * 60)
        assert kinds(acts) == ["notify"]
        assert eng.can_open(st, "SOL/USDT", T0 + 10 + 30 * 60 + 1)[0]

    def test_consecutive_losses_stop(self):
        eng = engine()
        st = eng.new_session(1, params(max_consecutive_losses=1, consecutive_loss_action="stop"), T0)
        pos = open_pos(eng, st)
        eng.record_close(st, "BTC/USDT", pos.cost_usdt - 0.05, 0.003)
        acts = eng.evaluate(st, {}, T0 + 10)
        assert acts[0].reason == StopReason.CONSECUTIVE_LOSSES and st.status == SessionStatus.ENDED


# ------------------------------------------------------------------ manual / emergency
class TestManual:
    def test_manual_end_close_all(self):
        eng = engine()
        st = eng.new_session(1, params(on_session_end="close_all"), T0)
        open_pos(eng, st)
        acts = eng.request_end(st, T0 + 5)
        assert kinds(acts) == ["close_all"] and acts[0].reason == StopReason.MANUAL

    def test_emergency_overrides_manage_only(self):
        eng = engine()
        st = eng.new_session(1, params(on_session_end="manage_only"), T0)
        open_pos(eng, st)
        acts = eng.emergency(st, T0 + 5)
        assert kinds(acts) == ["close_all"] and acts[0].reason == StopReason.EMERGENCY


# ------------------------------------------------------------------ restore after restart
class TestRestore:
    def test_roundtrip_through_json(self):
        eng = engine()
        st = eng.new_session(7, params(duration_hours=3), T0)
        pos = open_pos(eng, st)
        eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, 0.2)}, T0 + 60)
        blob = json.dumps(st.to_dict())
        st2 = SessionState.from_dict(json.loads(blob))
        assert st2.session_id == 7
        assert st2.started_at == T0 and st2.ends_at == T0 + 3 * 3600
        assert st2.positions["BTC/USDT"].cost_usdt == pytest.approx(pos.cost_usdt)
        assert st2.peak_pnl == pytest.approx(0.2)
        assert st2.trades_opened == 1 and st2.fees_paid == pytest.approx(st.fees_paid)

    def test_timer_continues_after_restart(self):
        """The deadline comes from the persisted start time, not from the restart time."""
        eng = engine()
        st = eng.new_session(1, params(duration_hours=2), T0)
        st2 = SessionState.from_dict(json.loads(json.dumps(st.to_dict())))
        # "computer slept" and restarted 3h later -> session must end, not restart its timer
        acts = eng.evaluate(st2, {}, T0 + 3 * 3600)
        assert st2.status == SessionStatus.ENDED and acts[0].reason == StopReason.TIME

    def test_limits_survive_restart(self):
        eng = engine()
        st = eng.new_session(1, params(max_loss_usdt=1), T0)
        pos = open_pos(eng, st)
        eng.record_close(st, "BTC/USDT", pos.cost_usdt - 0.8, 0.003)
        st2 = SessionState.from_dict(json.loads(json.dumps(st.to_dict())))
        pos2 = open_pos(eng, st2, "ETH/USDT")
        acts = eng.evaluate(st2, {"ETH/USDT": price_for_unrealized(pos2, -0.25)}, T0 + 100)
        assert acts[0].reason == StopReason.MAX_LOSS

    def test_closing_state_resumes_closing(self):
        eng = engine()
        st = eng.new_session(1, params(max_loss_usdt=0.2), T0)
        pos = open_pos(eng, st)
        eng.evaluate(st, {"BTC/USDT": price_for_unrealized(pos, -0.3)}, T0 + 1)
        st2 = SessionState.from_dict(json.loads(json.dumps(st.to_dict())))
        acts = eng.evaluate(st2, {"BTC/USDT": 100.0}, T0 + 2)
        assert kinds(acts) == ["close_all"] and acts[0].reason == StopReason.MAX_LOSS
