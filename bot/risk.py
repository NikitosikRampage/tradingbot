"""Session / risk engine.

Pure logic without any I/O: it is shared by the live trader, the backtester
and the unit tests. All timestamps are UTC epoch seconds (float).

PnL convention: ``Position.cost_usdt`` is the total amount of USDT spent on the
entry *including* the entry fee (no matter whether OKX charged the fee in the
base or the quote currency). ``Position.qty`` is the net base quantity held.
Unrealised PnL therefore is ``qty * price * (1 - taker_fee) - cost_usdt`` —
the estimated exit fee is included, so the session PnL is always net of fees.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any


class StopReason(str, Enum):
    TIME = "time"
    MAX_LOSS = "max_loss"
    PROFIT_TARGET = "profit_target"
    PROFIT_TRAIL = "profit_trail"
    MAX_TRADES = "max_trades"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    DAILY_LOSS_CAP = "daily_loss_cap"
    MANUAL = "manual"
    EMERGENCY = "emergency"


STOP_REASON_RU = {
    StopReason.TIME: "истекло время сессии",
    StopReason.MAX_LOSS: "достигнут лимит убытка",
    StopReason.PROFIT_TARGET: "достигнута цель по прибыли",
    StopReason.PROFIT_TRAIL: "трейлинг прибыли (profit_lock) — откат от пика",
    StopReason.MAX_TRADES: "исчерпан лимит сделок",
    StopReason.CONSECUTIVE_LOSSES: "серия убыточных сделок",
    StopReason.DAILY_LOSS_CAP: "дневной лимит убытка",
    StopReason.MANUAL: "ручная остановка",
    StopReason.EMERGENCY: "аварийная остановка (кнопка «Стоп» или файл STOP)",
}


class SessionStatus(str, Enum):
    ACTIVE = "active"            # торгует
    WINDING_DOWN = "winding_down"  # manage_only: новые не открываем, ждём SL/TP
    CLOSING = "closing"          # закрываем все позиции по рынку
    ENDED = "ended"


ON_END_MODES = ("close_all", "manage_only")
CONSEC_ACTIONS = ("pause", "stop")


# --------------------------------------------------------------------------- params
@dataclass
class SessionParams:
    budget_usdt: float = 10.0
    duration_hours: float = 8.0
    max_loss_usdt: float | None = 2.0
    max_loss_pct: float | None = None
    profit_target_usdt: float | None = None
    profit_target_pct: float | None = None
    profit_lock: bool = False
    profit_lock_trail_pct: float = 30.0
    max_trades: int = 10
    max_consecutive_losses: int = 3          # 0 = выключено
    consecutive_loss_action: str = "pause"   # pause | stop
    consecutive_loss_pause_minutes: float = 60.0
    on_session_end: str = "close_all"        # close_all | manage_only
    manage_only_max_minutes: float = 60.0
    no_entry_last_hours: float = 0.0         # не открывать новые позиции за N часов до конца сессии
                                             # (не больше половины длительности сессии)
    max_open_positions: int = 2
    position_size_pct: float = 30.0
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SessionParams":
        d = d or {}
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"Неизвестные параметры сессии: {', '.join(sorted(unknown))}")
        p = cls()
        for k, v in d.items():
            setattr(p, k, coerce_param(k, v))
        return p

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def replace(self, **kw: Any) -> "SessionParams":
        p = copy.deepcopy(self)
        for k, v in kw.items():
            if not hasattr(p, k):
                raise ValueError(f"Неизвестный параметр: {k}")
            setattr(p, k, coerce_param(k, v))
        return p


_FLOAT_OR_NONE = {"max_loss_usdt", "max_loss_pct", "profit_target_usdt", "profit_target_pct"}
_INT = {"max_trades", "max_consecutive_losses", "max_open_positions"}
_BOOL = {"profit_lock"}
_STR = {"on_session_end", "consecutive_loss_action"}


def coerce_param(name: str, value: Any) -> Any:
    """Convert a raw (string) value coming from YAML/the web UI to the right type."""
    if name in _BOOL:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on", "да", "вкл"):
            return True
        if s in ("0", "false", "no", "off", "нет", "выкл"):
            return False
        raise ValueError(f"{name}: ожидается on/off, получено {value!r}")
    if name in _STR:
        return str(value).strip().lower()
    if name in _FLOAT_OR_NONE:
        if value is None or str(value).strip().lower() in ("none", "off", "null", "-", ""):
            return None
        return float(value)
    if name in _INT:
        f = float(value)
        if f != int(f):
            raise ValueError(f"{name}: ожидается целое число")
        return int(f)
    return float(value)


@dataclass
class HardCaps:
    """Ceilings that can only be raised by editing config.yaml + restart."""
    max_budget_usdt: float = 50.0
    max_loss_usdt: float = 10.0
    max_duration_hours: float = 24.0
    max_trades_per_session: int = 50
    max_position_size_pct: float = 30.0
    max_open_positions: int = 2

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "HardCaps":
        d = d or {}
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"Неизвестные hard_caps: {', '.join(sorted(unknown))}")
        return cls(**{k: type(getattr(cls(), k))(v) for k, v in d.items()})


@dataclass
class DailyCaps:
    daily_budget_cap_usdt: float = 30.0
    daily_loss_cap_usdt: float = 5.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "DailyCaps":
        d = d or {}
        return cls(**{k: float(v) for k, v in d.items() if k in {f.name for f in fields(cls)}})


@dataclass
class DailyStats:
    """Aggregates over the current trading day (in the configured timezone)."""
    budget_allocated_usdt: float = 0.0   # сумма бюджетов сессий, запущенных сегодня (вкл. текущую)
    realized_pnl_other_usdt: float = 0.0  # PnL сделок, закрытых сегодня в ДРУГИХ сессиях


# --------------------------------------------------------------------------- state
@dataclass
class Position:
    symbol: str
    qty: float               # net base qty held
    entry_price: float
    cost_usdt: float         # USDT spent incl. entry fee
    entry_fee_usdt: float
    stop_loss: float
    take_profit: float
    opened_at: float
    trade_id: int | None = None
    algo_id: str | None = None   # exchange-side OCO order id
    highest: float = 0.0         # highest price seen since entry (trailing stop)
    initial_stop: float = 0.0    # stop-loss at entry (to tell SL from trailing exits)
    oco_stop: float = 0.0        # stop price of the currently placed OCO

    def value_net(self, price: float, fee_rate: float) -> float:
        return self.qty * price * (1.0 - fee_rate)

    def unrealized(self, price: float, fee_rate: float) -> float:
        return self.value_net(price, fee_rate) - self.cost_usdt


@dataclass
class SessionState:
    session_id: int
    params: SessionParams
    started_at: float
    ends_at: float
    status: SessionStatus = SessionStatus.ACTIVE
    stop_reason: StopReason | None = None
    ended_at: float | None = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    trades_opened: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0
    last_total_pnl: float = 0.0
    profit_lock_active: bool = False
    paused_until: float | None = None
    manual_paused: bool = False
    wind_down_deadline: float | None = None
    positions: dict[str, Position] = field(default_factory=dict)

    # ---- (de)serialisation for SQLite persistence / restore after restart
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["stop_reason"] = self.stop_reason.value if self.stop_reason else None
        d["params"] = self.params.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionState":
        d = dict(d)
        d["params"] = SessionParams.from_dict(d["params"])
        d["status"] = SessionStatus(d["status"])
        d["stop_reason"] = StopReason(d["stop_reason"]) if d.get("stop_reason") else None
        d["positions"] = {k: Position(**v) for k, v in (d.get("positions") or {}).items()}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def is_running(self) -> bool:
        return self.status != SessionStatus.ENDED

    @property
    def open_cost(self) -> float:
        return sum(p.cost_usdt for p in self.positions.values())

    @property
    def winrate(self) -> float:
        return self.wins / self.trades_closed if self.trades_closed else 0.0


@dataclass
class PnlSnapshot:
    realized: float
    unrealized: float
    total: float
    fees: float
    open_cost: float


@dataclass
class Action:
    kind: str                 # close_all | end | notify
    reason: StopReason | None = None
    message: str = ""


# --------------------------------------------------------------------------- engine
class RiskEngine:
    MIN_ORDER_USDT = 1.0

    def __init__(self, caps: HardCaps, daily_caps: DailyCaps, fee_rate: float = 0.001):
        self.caps = caps
        self.daily_caps = daily_caps
        self.fee_rate = fee_rate

    # ---------------------------------------------------------------- validation
    def validate_params(self, p: SessionParams) -> list[str]:
        c = self.caps
        e: list[str] = []
        if not (0 < p.budget_usdt <= c.max_budget_usdt):
            e.append(f"budget_usdt={p.budget_usdt} вне (0; {c.max_budget_usdt}] (hard_caps.max_budget_usdt)")
        if not (0 < p.duration_hours <= c.max_duration_hours):
            e.append(f"duration_hours={p.duration_hours} вне (0; {c.max_duration_hours}] (hard_caps.max_duration_hours)")
        if p.max_loss_usdt is not None and not (0 < p.max_loss_usdt <= c.max_loss_usdt):
            e.append(f"max_loss_usdt={p.max_loss_usdt} вне (0; {c.max_loss_usdt}] (hard_caps.max_loss_usdt)")
        if p.max_loss_pct is not None and not (0 < p.max_loss_pct <= 100):
            e.append("max_loss_pct должен быть в (0; 100]")
        if p.profit_target_usdt is not None and p.profit_target_usdt <= 0:
            e.append("profit_target_usdt должен быть > 0")
        if p.profit_target_pct is not None and p.profit_target_pct <= 0:
            e.append("profit_target_pct должен быть > 0")
        if not (0 < p.profit_lock_trail_pct < 100):
            e.append("profit_lock_trail_pct должен быть в (0; 100)")
        if not (1 <= p.max_trades <= c.max_trades_per_session):
            e.append(f"max_trades={p.max_trades} вне [1; {c.max_trades_per_session}] (hard_caps.max_trades_per_session)")
        if p.max_consecutive_losses < 0:
            e.append("max_consecutive_losses должен быть >= 0")
        if p.consecutive_loss_action not in CONSEC_ACTIONS:
            e.append(f"consecutive_loss_action: одно из {CONSEC_ACTIONS}")
        if p.consecutive_loss_pause_minutes <= 0:
            e.append("consecutive_loss_pause_minutes должен быть > 0")
        if p.on_session_end not in ON_END_MODES:
            e.append(f"on_session_end: одно из {ON_END_MODES}")
        if p.no_entry_last_hours < 0:
            e.append("no_entry_last_hours должен быть >= 0")
        if p.manage_only_max_minutes <= 0:
            e.append("manage_only_max_minutes должен быть > 0")
        if not (1 <= p.max_open_positions <= c.max_open_positions):
            e.append(f"max_open_positions вне [1; {c.max_open_positions}] (hard_caps.max_open_positions)")
        if not (0 < p.position_size_pct <= c.max_position_size_pct):
            e.append(f"position_size_pct вне (0; {c.max_position_size_pct}] (hard_caps.max_position_size_pct)")
        if not (0 < p.stop_loss_pct < 50):
            e.append("stop_loss_pct должен быть в (0; 50)")
        if not (0 < p.take_profit_pct < 500):
            e.append("take_profit_pct должен быть в (0; 500)")
        min_tp = 3 * 2 * self.fee_rate * 100  # TP must cover the round-trip fees with a margin
        if p.take_profit_pct <= min_tp:
            e.append(f"take_profit_pct={p.take_profit_pct} слишком мал: должен быть > {min_tp:.2f}% "
                     f"(3× комиссия туда-обратно)")
        return e

    def check_start(self, p: SessionParams, free_usdt: float | None, daily: DailyStats,
                    has_active: bool) -> list[str]:
        errors = self.validate_params(p)
        if has_active:
            errors.append("Уже есть активная сессия — сначала завершите её")
        if daily.budget_allocated_usdt + p.budget_usdt > self.daily_caps.daily_budget_cap_usdt + 1e-9:
            errors.append(
                f"Превышен daily_budget_cap_usdt: уже выделено {daily.budget_allocated_usdt:.2f}, "
                f"+{p.budget_usdt:.2f} > {self.daily_caps.daily_budget_cap_usdt:.2f}")
        if daily.realized_pnl_other_usdt <= -self.daily_caps.daily_loss_cap_usdt:
            errors.append(f"Дневной лимит убытка уже достигнут ({daily.realized_pnl_other_usdt:.2f} USDT)")
        if free_usdt is None:
            errors.append("Не удалось получить баланс с биржи")
        elif free_usdt + 1e-9 < p.budget_usdt:
            errors.append(f"Свободный баланс {free_usdt:.2f} USDT меньше бюджета {p.budget_usdt:.2f} USDT")
        return errors

    def effective_max_loss(self, p: SessionParams) -> float:
        cands = [self.caps.max_loss_usdt]
        if p.max_loss_usdt is not None:
            cands.append(p.max_loss_usdt)
        if p.max_loss_pct is not None:
            cands.append(p.budget_usdt * p.max_loss_pct / 100.0)
        return min(cands)

    @staticmethod
    def profit_target(p: SessionParams) -> float | None:
        cands = []
        if p.profit_target_usdt is not None:
            cands.append(p.profit_target_usdt)
        if p.profit_target_pct is not None:
            cands.append(p.budget_usdt * p.profit_target_pct / 100.0)
        return min(cands) if cands else None

    # ---------------------------------------------------------------- lifecycle
    def new_session(self, session_id: int, p: SessionParams, now: float) -> SessionState:
        errs = self.validate_params(p)
        if errs:
            raise ValueError("; ".join(errs))
        return SessionState(session_id=session_id, params=p, started_at=now,
                            ends_at=now + p.duration_hours * 3600.0)

    def update_param(self, state: SessionState, name: str, value: Any) -> SessionParams:
        """Change one parameter of a running session (validated against hard caps)."""
        newp = state.params.replace(**{name: value})
        errs = self.validate_params(newp)
        if errs:
            raise ValueError("; ".join(errs))
        state.params = newp
        if name == "duration_hours" and state.status == SessionStatus.ACTIVE:
            state.ends_at = state.started_at + newp.duration_hours * 3600.0
        return newp

    # ---------------------------------------------------------------- pnl
    def pnl(self, state: SessionState, prices: dict[str, float]) -> PnlSnapshot:
        unreal = 0.0
        for sym, pos in state.positions.items():
            price = prices.get(sym) or pos.entry_price
            unreal += pos.unrealized(price, self.fee_rate)
        return PnlSnapshot(realized=state.realized_pnl, unrealized=unreal,
                           total=state.realized_pnl + unreal, fees=state.fees_paid,
                           open_cost=state.open_cost)

    # ---------------------------------------------------------------- entries
    def can_open(self, state: SessionState, symbol: str, now: float,
                 data_fresh: bool = True) -> tuple[bool, str, float]:
        """Return (allowed, reason, max_cost_usdt)."""
        p = state.params
        if state.status != SessionStatus.ACTIVE:
            return False, f"сессия в статусе {state.status.value}", 0.0
        if now >= state.ends_at:
            return False, "время сессии истекло", 0.0
        cutoff = min(p.no_entry_last_hours, p.duration_hours / 2)
        if cutoff > 0 and now >= state.ends_at - cutoff * 3600:
            return False, f"последние {cutoff:g}ч сессии — новые входы не открываются", 0.0
        if state.manual_paused:
            return False, "пауза (включена вручную)", 0.0
        if state.paused_until and now < state.paused_until:
            return False, "пауза после серии убытков", 0.0
        if not data_fresh:
            return False, "данные устарели", 0.0
        if state.trades_opened >= p.max_trades:
            return False, "лимит сделок за сессию", 0.0
        if symbol in state.positions:
            return False, "уже есть позиция по монете", 0.0
        if len(state.positions) >= p.max_open_positions:
            return False, "максимум одновременных позиций", 0.0
        # Прибыль не реинвестируется сверх бюджета, убытки уменьшают доступный капитал.
        capital = min(p.budget_usdt, p.budget_usdt + state.realized_pnl)
        available = capital - state.open_cost
        size = min(p.budget_usdt * p.position_size_pct / 100.0, available)
        if size < self.MIN_ORDER_USDT:
            return False, f"недостаточно бюджета сессии ({available:.2f} USDT)", 0.0
        return True, "ok", round(size, 8)

    def record_open(self, state: SessionState, pos: Position) -> None:
        state.positions[pos.symbol] = pos
        state.trades_opened += 1
        state.fees_paid += pos.entry_fee_usdt

    def record_close(self, state: SessionState, symbol: str, proceeds_net: float,
                     exit_fee_usdt: float) -> float:
        """``proceeds_net`` — USDT actually received after the exit fee."""
        pos = state.positions.pop(symbol)
        pnl = proceeds_net - pos.cost_usdt
        state.realized_pnl += pnl
        state.fees_paid += exit_fee_usdt
        state.trades_closed += 1
        if pnl > 0:
            state.wins += 1
            state.consecutive_losses = 0
        else:
            state.losses += 1
            state.consecutive_losses += 1
        return pnl

    # ---------------------------------------------------------------- stopping
    def finish(self, state: SessionState, reason: StopReason, now: float) -> None:
        state.status = SessionStatus.ENDED
        state.stop_reason = state.stop_reason or reason
        state.ended_at = now

    def _begin_close(self, state: SessionState, reason: StopReason, now: float) -> list[Action]:
        if state.status != SessionStatus.CLOSING:
            state.stop_reason = reason
        state.status = SessionStatus.CLOSING
        if not state.positions:
            self.finish(state, reason, now)
            return [Action("end", state.stop_reason)]
        return [Action("close_all", state.stop_reason)]

    def _session_over(self, state: SessionState, reason: StopReason, now: float) -> list[Action]:
        """Normal end (time, manual, ...) — honours ``on_session_end``."""
        if state.params.on_session_end == "close_all" or not state.positions:
            return self._begin_close(state, reason, now)
        state.status = SessionStatus.WINDING_DOWN
        state.stop_reason = reason
        state.wind_down_deadline = now + state.params.manage_only_max_minutes * 60.0
        return [Action("notify", reason,
                       f"Сессия завершается ({STOP_REASON_RU[reason]}): новые сделки не открываются, "
                       f"открытые позиции ведутся до SL/TP не дольше "
                       f"{state.params.manage_only_max_minutes:.0f} мин.")]

    def request_end(self, state: SessionState, now: float,
                    reason: StopReason = StopReason.MANUAL) -> list[Action]:
        if state.status in (SessionStatus.ENDED, SessionStatus.CLOSING):
            return []
        if state.status == SessionStatus.WINDING_DOWN:
            return []
        return self._session_over(state, reason, now)

    def emergency(self, state: SessionState, now: float) -> list[Action]:
        if state.status == SessionStatus.ENDED:
            return []
        state.stop_reason = StopReason.EMERGENCY
        return self._begin_close(state, StopReason.EMERGENCY, now)

    def evaluate(self, state: SessionState, prices: dict[str, float], now: float,
                 daily: DailyStats | None = None) -> list[Action]:
        """Run on every tick. Mutates ``state`` and returns actions for the executor."""
        if state.status == SessionStatus.ENDED:
            return []
        p = state.params
        snap = self.pnl(state, prices)
        total = snap.total
        state.last_total_pnl = total
        state.peak_pnl = max(state.peak_pnl, total)
        state.max_drawdown = max(state.max_drawdown, state.peak_pnl - total)

        if state.status == SessionStatus.CLOSING:
            if not state.positions:
                self.finish(state, state.stop_reason or StopReason.MANUAL, now)
                return [Action("end", state.stop_reason)]
            return [Action("close_all", state.stop_reason)]

        actions: list[Action] = []

        # 1. Дневной лимит убытка (по всем сессиям)
        if daily is not None and daily.realized_pnl_other_usdt + total <= -self.daily_caps.daily_loss_cap_usdt:
            return self._begin_close(state, StopReason.DAILY_LOSS_CAP, now)

        # 2. Лимит убытка сессии (реализованный + нереализованный, с комиссиями)
        if total <= -self.effective_max_loss(p):
            return self._begin_close(state, StopReason.MAX_LOSS, now)

        # 3. Цель по прибыли / profit lock
        target = self.profit_target(p)
        if target is not None and total >= target:
            if not p.profit_lock:
                return self._begin_close(state, StopReason.PROFIT_TARGET, now)
            if not state.profit_lock_active:
                state.profit_lock_active = True
                actions.append(Action("notify", None,
                                      f"🎯 Цель {target:.2f} USDT достигнута (PnL {total:.2f}). "
                                      f"Включён трейлинг прибыли: стоп при откате на "
                                      f"{p.profit_lock_trail_pct:.0f}% от пика."))
        if state.profit_lock_active:
            floor = state.peak_pnl * (1.0 - p.profit_lock_trail_pct / 100.0)
            if total <= floor:
                return actions + self._begin_close(state, StopReason.PROFIT_TRAIL, now)

        # 4. manage_only — доводим позиции
        if state.status == SessionStatus.WINDING_DOWN:
            if not state.positions:
                self.finish(state, state.stop_reason or StopReason.TIME, now)
                return actions + [Action("end", state.stop_reason)]
            if state.wind_down_deadline is not None and now >= state.wind_down_deadline:
                return actions + self._begin_close(state, state.stop_reason or StopReason.TIME, now)
            return actions

        # 5. Таймер
        if now >= state.ends_at:
            return actions + self._session_over(state, StopReason.TIME, now)

        # 6. Лимит сделок: ждём закрытия последних позиций и завершаем
        if state.trades_opened >= p.max_trades and not state.positions:
            self.finish(state, StopReason.MAX_TRADES, now)
            return actions + [Action("end", StopReason.MAX_TRADES)]

        # 7. Серия убытков
        if p.max_consecutive_losses and state.consecutive_losses >= p.max_consecutive_losses:
            if p.consecutive_loss_action == "stop":
                return actions + self._session_over(state, StopReason.CONSECUTIVE_LOSSES, now)
            state.paused_until = now + p.consecutive_loss_pause_minutes * 60.0
            state.consecutive_losses = 0
            actions.append(Action("notify", None,
                                  f"⏸ {p.max_consecutive_losses} убыточных сделок подряд — пауза "
                                  f"{p.consecutive_loss_pause_minutes:.0f} мин."))
        if state.paused_until is not None and now >= state.paused_until:
            state.paused_until = None
            actions.append(Action("notify", None, "▶️ Пауза после серии убытков закончилась."))
        return actions

    # ---------------------------------------------------------------- reporting
    def distances(self, state: SessionState, prices: dict[str, float], now: float) -> dict[str, Any]:
        snap = self.pnl(state, prices)
        p = state.params
        target = self.profit_target(p)
        return {
            "pnl": snap,
            "time_left_sec": max(0.0, state.ends_at - now),
            "to_max_loss": snap.total + self.effective_max_loss(p),
            "max_loss": self.effective_max_loss(p),
            "to_target": (target - snap.total) if target is not None else None,
            "target": target,
            "trades_left": max(0, p.max_trades - state.trades_opened),
            "budget_free": min(p.budget_usdt, p.budget_usdt + state.realized_pnl) - state.open_cost,
        }
