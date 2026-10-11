"""Trader: sessions, entries, SL/TP, limits on every tick, reconciliation and restore.

The trading rules live in ``risk.RiskEngine`` (pure); this module performs the I/O.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets as pysecrets
import time
from datetime import datetime
from typing import Any, Callable

import ccxt.async_support as ccxt

from .collector import MarketState
from .config import Config
from .db import Database
from .exchange import Exchange, new_client_id
from .health import Health
from .exits import ExitConfig, check_exit, raise_stop
from .marketdata import closes, closes_4h
from .protections import ClosedTrade, ProtectionConfig, compute_locks, entry_blocked
from .risk import (STOP_REASON_RU, Action, DailyStats, Position, SessionParams, SessionState,
                   SessionStatus, StopReason)
from .strategy import StrategyParams, generate_signal

log = logging.getLogger(__name__)

PARAM_ALIASES = {
    "budget": "budget_usdt", "hours": "duration_hours", "duration": "duration_hours",
    "max_loss": "max_loss_usdt", "loss": "max_loss_usdt", "max_loss_pct": "max_loss_pct",
    "target": "profit_target_usdt", "target_pct": "profit_target_pct",
    "lock": "profit_lock", "profit_lock": "profit_lock", "trail": "profit_lock_trail_pct",
    "trades": "max_trades", "max_trades": "max_trades",
    "losses": "max_consecutive_losses", "max_consecutive_losses": "max_consecutive_losses",
    "on_end": "on_session_end", "end": "on_session_end", "on_session_end": "on_session_end",
    "manage_minutes": "manage_only_max_minutes", "sl": "stop_loss_pct", "tp": "take_profit_pct",
    "size": "position_size_pct", "positions": "max_open_positions", "cutoff": "no_entry_last_hours",
}


def resolve_param(name: str) -> str:
    n = name.strip().lower()
    if n in PARAM_ALIASES:
        return PARAM_ALIASES[n]
    if n in SessionParams().to_dict():
        return n
    raise ValueError(f"Неизвестный параметр: {name}")


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}ч {m:02d}м"


class Trader:
    def __init__(self, cfg: Config, db: Database, ex: Exchange, market: MarketState,
                 health: Health, notify: Callable[[str], None]):
        self.cfg = cfg
        self.db = db
        self.ex = ex
        self.market = market
        self.health = health
        self._notify = notify
        self.engine = cfg.risk_engine()
        self.strategy = StrategyParams.from_dict(cfg["trader"]["strategy"])
        self.stale_after = float(cfg["collector"]["stale_after_sec"])
        self.tick_interval = float(cfg["trader"]["tick_interval_sec"])
        self.exchange_sl_tp = bool(cfg["trader"]["exchange_side_sl_tp"])
        self.exits = ExitConfig.from_dict(cfg["trader"].get("exits"))
        self.protections = ProtectionConfig.from_dict(cfg["trader"].get("protections"))
        # replaced by the dynamic pairlist when it is enabled (see app.py)
        self.trading_symbols: Callable[[], list[str]] = lambda: list(cfg.symbols)
        self._oco_refresh_at: dict[str, float] = {}
        self.stop_file = cfg.path("stop_file")
        self.state: SessionState | None = None
        self.lock = asyncio.Lock()
        self.live_code: str | None = None
        self._last_persist = 0.0
        self._last_algo_poll = 0.0
        self._last_close_attempt = 0.0
        self._rest_prices: dict[str, float] = {}
        self._rest_prices_at = 0.0
        self._stale_notified = False
        self._daily_cache: tuple[float, int | None, DailyStats] | None = None

    # ================================================================== flags
    @property
    def mode(self) -> str:
        return self.ex.mode

    def notify(self, text: str) -> None:
        self._notify(f"[{self.mode}] {text}")

    @property
    def enabled(self) -> bool:
        return bool(self.db.kv_get("trader_enabled", self.cfg["trader"]["enabled"]))

    def set_enabled(self, value: bool) -> None:
        self.db.kv_set("trader_enabled", bool(value))

    @property
    def paused(self) -> bool:
        return bool(self.db.kv_get("trader_paused", False))

    def _key_fingerprint(self) -> str:
        return hashlib.sha256(self.cfg.secrets.okx_key.encode()).hexdigest()[:16]

    @property
    def live_confirmed(self) -> bool:
        if not self.ex.live:
            return True
        return self.db.kv_get("live_confirmed") == self._key_fingerprint()

    def request_live_confirmation(self) -> None:
        self.live_code = f"{pysecrets.randbelow(10 ** 6):06d}"
        self._notify("⚠️ В .env включён OKX_LIVE_TRADING=true (РЕАЛЬНЫЙ счёт).\n"
                     f"Торговля заблокирована до подтверждения: /confirm_live {self.live_code}\n"
                     "Если это ошибка — выключите флаг в .env и перезапустите бота.")

    def confirm_live(self, code: str) -> str:
        if not self.ex.live:
            return "Бот работает в DEMO. Для реала: OKX_LIVE_TRADING=true в .env + перезапуск."
        if not self.live_code or code.strip() != self.live_code:
            return "Неверный код подтверждения."
        self.db.kv_set("live_confirmed", self._key_fingerprint())
        self.live_code = None
        return "✅ Реальная торговля подтверждена для текущего API-ключа."

    def blockers(self) -> list[str]:
        b = []
        if not self.enabled:
            b.append("trader выключен (/trader_on)")
        if not self.ex.can_trade:
            b.append("нет API-ключей OKX в .env")
        if not self.live_confirmed:
            b.append("реальный счёт не подтверждён (/confirm_live)")
        if self.stop_file.exists():
            b.append(f"существует файл {self.stop_file.name}")
        return b

    # ================================================================== persistence
    def persist(self) -> None:
        if self.state is not None:
            self.db.save_session_state(self.state.to_dict())
            self._last_persist = time.time()

    def _day_start(self, now: float | None = None) -> float:
        dt = datetime.fromtimestamp(now or time.time(), self.cfg.tz)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def daily_stats(self, exclude_session: int | None = None, fresh: bool = False) -> DailyStats:
        now = time.time()
        if (not fresh and self._daily_cache and now - self._daily_cache[0] < 60
                and self._daily_cache[1] == exclude_session):
            return self._daily_cache[2]
        start = self._day_start(now)
        # all sessions started today, including the current one
        budgets = sum(float(r["budget_usdt"] or 0) for r in self.db.sessions_since(start))
        ds = DailyStats(budget_allocated_usdt=budgets,
                        realized_pnl_other_usdt=self.db.realized_pnl_between(
                            start, now + 1, exclude_session=exclude_session))
        self._daily_cache = (now, exclude_session, ds)
        return ds

    # ================================================================== session control
    def parse_overrides(self, kv: dict[str, str]) -> dict[str, Any]:
        return {resolve_param(k): v for k, v in kv.items()}

    async def start_session(self, overrides: dict[str, Any], source: str = "вручную") -> str:
        async with self.lock:
            if self.state is not None:
                return "Уже есть активная сессия. /session_status или /session_end"
            blockers = self.blockers()
            if blockers:
                msg = "Сессия не запущена: " + "; ".join(blockers)
                return msg
            try:
                params = self.cfg.session_defaults.replace(**overrides)
            except ValueError as exc:
                return f"Ошибка параметров: {exc}"
            try:
                free = await self.ex.free_usdt()
            except Exception as exc:  # noqa: BLE001
                log.warning("free_usdt: %s", exc)
                free = None
            errors = self.engine.check_start(params, free, self.daily_stats(fresh=True), has_active=False)
            if errors:
                msg = "❌ Сессия не запущена:\n- " + "\n- ".join(errors)
                self.notify(msg)
                return msg
            now = time.time()
            sid = self.db.create_session(now, now + params.duration_hours * 3600, self.mode.lower(),
                                         params.budget_usdt, params.to_dict())
            self.state = self.engine.new_session(sid, params, now)
            self.state.manual_paused = self.paused
            self._daily_cache = None
            self.persist()
            end_local = datetime.fromtimestamp(self.state.ends_at, self.cfg.tz)
            target = self.engine.profit_target(params)
            msg = (f"▶️ Сессия #{sid} запущена ({source}).\n"
                   f"Бюджет {params.budget_usdt:.2f} USDT (свободно на счёте {free:.2f}), до {end_local:%d.%m %H:%M}\n"
                   f"Макс. убыток {self.engine.effective_max_loss(params):.2f} USDT, цель "
                   f"{f'{target:.2f} USDT' if target is not None else '—'}"
                   f"{' (profit_lock, трейл ' + format(params.profit_lock_trail_pct, '.0f') + '%)' if params.profit_lock else ''}\n"
                   f"Сделок ≤ {params.max_trades}, позиций ≤ {params.max_open_positions}, "
                   f"размер ≤ {params.position_size_pct:.0f}% бюджета, SL {params.stop_loss_pct}% / TP {params.take_profit_pct}%\n"
                   f"По окончании: {params.on_session_end}")
            self.notify(msg)
            return msg

    async def set_param(self, name: str, value: str) -> str:
        async with self.lock:
            if self.state is None:
                return "Нет активной сессии."
            try:
                key = resolve_param(name)
                old = getattr(self.state.params, key)
                if key == "budget_usdt":
                    newb = float(value)
                    d = self.daily_stats(fresh=True)
                    if d.budget_allocated_usdt - old + newb > self.engine.daily_caps.daily_budget_cap_usdt + 1e-9:
                        return "Превышен daily_budget_cap_usdt."
                    if newb > old:
                        free = await self.ex.free_usdt()
                        if free + self.state.open_cost < newb:
                            return f"Свободный баланс {free:.2f} USDT недостаточен для бюджета {newb:.2f}."
                self.engine.update_param(self.state, key, value)
                if key == "budget_usdt":
                    self.db.execute("UPDATE sessions SET budget_usdt=? WHERE id=?",
                                    (self.state.params.budget_usdt, self.state.session_id))
            except ValueError as exc:
                return f"❌ {exc}"
            self.persist()
            return f"✅ {key}: {old} → {getattr(self.state.params, key)}"

    async def end_session(self, reason: StopReason = StopReason.MANUAL) -> str:
        async with self.lock:
            if self.state is None:
                return "Нет активной сессии."
            actions = self.engine.request_end(self.state, time.time(), reason)
            await self._execute(actions)
            self.persist()
            if self.state is None:
                return "Сессия завершена."
            return f"Сессия завершается (режим {self.state.params.on_session_end})."

    async def emergency_stop(self, source: str) -> str:
        self.set_enabled(False)
        async with self.lock:
            if self.state is not None:
                actions = self.engine.emergency(self.state, time.time())
                self._last_close_attempt = 0
                await self._execute(actions)
                self.persist()
        msg = f"🛑 АВАРИЙНАЯ ОСТАНОВКА ({source}): позиции закрываются по рынку, trader выключен."
        self.notify(msg)
        return msg

    def set_paused(self, paused: bool) -> str:
        self.db.kv_set("trader_paused", paused)
        if self.state is not None:
            self.state.manual_paused = paused
            self.persist()
        return "⏸ Новые входы на паузе." if paused else "▶️ Входы возобновлены."

    # ================================================================== main loop
    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.market.tick_event.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                pass
            self.market.tick_event.clear()
            self.health.beat("trader")
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("trader tick")
                self.health.error("trader", str(exc))
            await asyncio.sleep(0.25)

    async def tick(self) -> None:
        if self.stop_file.exists() and (self.enabled or self.state is not None):
            if not (self.state and self.state.status == SessionStatus.CLOSING
                    and self.state.stop_reason == StopReason.EMERGENCY):
                await self.emergency_stop(f"файл {self.stop_file.name}")
        if self.state is None:
            return
        async with self.lock:
            if self.state is None:
                return
            prices = await self._prices()
            await self._check_sl_tp(prices)
            if self.state is None:
                return
            daily = self.daily_stats(exclude_session=self.state.session_id)
            actions = self.engine.evaluate(self.state, prices, time.time(), daily)
            await self._execute(actions)
            if self.state is not None and time.time() - self._last_algo_poll > 30:
                self._last_algo_poll = time.time()
                await self._poll_algos()
            if self.state is not None and time.time() - self._last_persist > 15:
                self.persist()

    async def _prices(self) -> dict[str, float]:
        assert self.state is not None
        prices = self.market.prices()
        stale = [s for s in self.state.positions if not self.market.fresh(s, self.stale_after)]
        all_stale = all(not self.market.fresh(s, self.stale_after) for s in self.cfg.symbols)
        if all_stale and not self._stale_notified and self.state.status == SessionStatus.ACTIVE:
            self._stale_notified = True
            self.notify(f"⚠️ Нет тиков > {self.stale_after:.0f} сек — новые входы приостановлены.")
        elif not all_stale and self._stale_notified:
            self._stale_notified = False
            self.notify("✅ Поток данных восстановлен.")
        if stale:
            # REST fallback so that limits/SL keep working while the WS is down
            if time.time() - self._rest_prices_at > 10:
                self._rest_prices_at = time.time()
                try:
                    t = await self.ex.fetch_tickers(stale)
                    self._rest_prices.update({s: float(v["last"]) for s, v in t.items() if v.get("last")})
                except Exception as exc:  # noqa: BLE001
                    log.warning("REST tickers fallback: %s", exc)
            for s in stale:
                if s in self._rest_prices:
                    prices[s] = self._rest_prices[s]
        return prices

    async def _execute(self, actions: list[Action]) -> None:
        for a in actions:
            if a.kind == "notify":
                self.notify(a.message)
            elif a.kind == "close_all":
                if time.time() - self._last_close_attempt < 5:
                    continue
                self._last_close_attempt = time.time()
                reason = STOP_REASON_RU.get(a.reason, str(a.reason)) if a.reason else "закрытие сессии"
                if self.state and self.state.positions:
                    self.notify(f"⏹ Закрываю все позиции: {reason}")
                for sym in list(self.state.positions if self.state else []):
                    await self._close_position(sym, f"закрытие сессии: {reason}")
                if self.state is not None and not self.state.positions:
                    self.engine.finish(self.state, a.reason or StopReason.MANUAL, time.time())
                    await self._finish_session()
            elif a.kind == "end":
                await self._finish_session()

    # ================================================================== entries
    async def on_candle_closed(self, symbol: str, tf: str, row: list[float]) -> None:
        if tf != "1h" or self.state is None or symbol not in self.trading_symbols():
            return
        await self.try_enter(symbol)

    async def try_enter(self, symbol: str) -> None:
        async with self.lock:
            st = self.state
            if st is None or self.blockers():
                return
            now = time.time()
            ok, reason, size = self.engine.can_open(st, symbol, now,
                                                    data_fresh=self.market.fresh(symbol, self.stale_after))
            if not ok:
                log.debug("entry %s blocked: %s", symbol, reason)
                return
            blocked, why = entry_blocked(self.closed_trades(now), self.protections, symbol, now)
            if blocked:
                log.info("entry %s blocked by protection: %s", symbol, why)
                return
            sig = generate_signal(symbol, closes(self.db, symbol, "1h", 300),
                                  closes_4h(self.db, symbol, self.strategy.ema_slow + 60), self.strategy)
            if sig is None:
                return
            try:
                free = await self.ex.free_usdt()
            except Exception as exc:  # noqa: BLE001
                log.warning("free_usdt: %s", exc)
                return
            if free < size:
                self.notify(f"⚠️ {symbol}: сигнал есть, но свободных USDT ({free:.2f}) меньше размера позиции {size:.2f}.")
                return
            cid = new_client_id()
            trade_id = self.db.insert_trade(session_id=st.session_id, symbol=symbol, mode=self.mode.lower(),
                                            status="pending", client_order_id=cid, entry_ts=now,
                                            entry_reason=sig.reason)
            try:
                fill = await self.ex.market_buy_cost(symbol, size, cid)
            except Exception as exc:  # noqa: BLE001
                log.exception("buy %s", symbol)
                self.db.update_trade(trade_id, status="failed", exit_reason=f"buy error: {exc}"[:500])
                self.notify(f"❌ Ошибка покупки {symbol}: {exc}")
                return
            await self._register_fill(st, trade_id, symbol, fill, sig.reason)

    async def _register_fill(self, st: SessionState, trade_id: int, symbol: str, fill: Any, reason: str) -> None:
        if fill.filled_base <= 0:
            self.db.update_trade(trade_id, status="failed", exit_reason=f"not filled ({fill.status})")
            self.notify(f"❌ Ордер {symbol} не исполнен ({fill.status}).")
            return
        p = st.params
        qty = fill.filled_base - fill.fee_base
        cost = fill.quote_amount + fill.fee_quote
        entry = fill.avg_price
        pos = Position(symbol=symbol, qty=qty, entry_price=entry, cost_usdt=cost,
                       entry_fee_usdt=fill.fee_usdt, stop_loss=entry * (1 - p.stop_loss_pct / 100),
                       take_profit=entry * (1 + p.take_profit_pct / 100), opened_at=time.time(),
                       trade_id=trade_id, highest=entry)
        pos.initial_stop = pos.stop_loss
        self.engine.record_open(st, pos)
        if self.exchange_sl_tp:
            try:
                pos.algo_id = await self.ex.place_oco(symbol, qty, pos.take_profit, pos.stop_loss)
                pos.oco_stop = pos.stop_loss
            except Exception as exc:  # noqa: BLE001
                log.warning("OCO %s: %s", symbol, exc)
                self.notify(f"⚠️ {symbol}: не удалось выставить OCO на бирже ({exc}). SL/TP контролирует бот.")
        self.db.update_trade(trade_id, status="open", order_id=fill.order_id, algo_id=pos.algo_id,
                             entry_price=entry, qty=qty, cost_usdt=cost, entry_fee_usdt=fill.fee_usdt,
                             stop_loss=pos.stop_loss, take_profit=pos.take_profit, entry_ts=pos.opened_at)
        self.persist()
        self.notify(f"🟢 ВХОД {symbol} (сессия #{st.session_id}, сделка {st.trades_opened}/{p.max_trades})\n"
                    f"Цена {entry:.6g}, кол-во {qty:.8g}, потрачено {cost:.4f} USDT, комиссия {fill.fee_usdt:.4f}\n"
                    f"SL {pos.stop_loss:.6g} (-{p.stop_loss_pct}%), TP {pos.take_profit:.6g} (+{p.take_profit_pct}%)"
                    f"{' [OCO на бирже]' if pos.algo_id else ''}\nПричина: {reason}")

    # ================================================================== exits
    async def _check_sl_tp(self, prices: dict[str, float]) -> None:
        st = self.state
        if st is None:
            return
        now = time.time()
        for sym, pos in list(st.positions.items()):
            price = prices.get(sym)
            if price is None:
                continue
            hit = check_exit(pos, price, now, self.exits)
            if hit is not None:
                await self._close_position(sym, hit[1], hit[0])
                continue
            if raise_stop(pos, price, self.exits, self.engine.fee_rate):
                self.persist()
                await self._refresh_oco(sym)

    async def _refresh_oco(self, sym: str) -> None:
        """Move the exchange-side OCO up after the trailing/break-even stop was raised."""
        st = self.state
        if st is None or sym not in st.positions or not self.exchange_sl_tp:
            return
        pos = st.positions[sym]
        if not pos.algo_id or pos.stop_loss < pos.oco_stop * 1.003:
            return
        if time.time() - self._oco_refresh_at.get(sym, 0) < 30:
            return
        self._oco_refresh_at[sym] = time.time()
        try:
            await self.ex.cancel_algo(sym, pos.algo_id)
            pos.algo_id = None
            await asyncio.sleep(0.5)
            if await self.ex.base_total(sym) < pos.qty * 0.95:
                await self._reconcile_position(sym)  # OCO fired before we could cancel it
                return
            pos.algo_id = await self.ex.place_oco(sym, pos.qty, pos.take_profit, pos.stop_loss)
            pos.oco_stop = pos.stop_loss
        except Exception as exc:  # noqa: BLE001
            log.warning("OCO refresh %s: %s", sym, exc)
            self.notify(f"⚠️ {sym}: не удалось переставить OCO ({exc}). Стоп {pos.stop_loss:.6g} ведёт бот.")
        self.persist()

    def closed_trades(self, now: float | None = None) -> list[ClosedTrade]:
        """Closed trades of ALL sessions in the protections lookback window."""
        now = now or time.time()
        since = now - self.protections.max_lookback_sec
        return [ClosedTrade(r["symbol"], float(r["exit_ts"]), float(r["pnl_usdt"] or 0),
                            float(r["pnl_pct"] or 0), r["exit_kind"] or "other")
                for r in self.db.trades("status='closed' AND exit_ts>=?", (since,))]

    def protections_text(self) -> str:
        if not self.protections.enabled:
            return "Защиты (protections) выключены в config.yaml."
        now = time.time()
        locks = compute_locks(self.closed_trades(now), self.protections, now)
        if not locks:
            return "🛡 Активных блокировок нет."
        return "🛡 Активные блокировки:\n" + "\n".join(
            f"• {lk.reason} — до {datetime.fromtimestamp(lk.until, self.cfg.tz):%d.%m %H:%M}" for lk in locks)

    async def _close_position(self, sym: str, reason: str, kind: str = "session") -> bool:
        st = self.state
        if st is None or sym not in st.positions:
            return True
        pos = st.positions[sym]
        try:
            if pos.algo_id:
                await self.ex.cancel_algo(sym, pos.algo_id)
                pos.algo_id = None
                await asyncio.sleep(0.5)
            held = await self.ex.base_free(sym)
            if held < pos.qty * 0.5:
                if await self.ex.base_total(sym) >= pos.qty * 0.95:
                    return False  # still frozen by the cancelled OCO -> retry on the next tick
                # already sold on the exchange (OCO triggered or manual)
                return await self._reconcile_position(sym)
            qty = min(pos.qty, held)
            fill = await self.ex.market_sell(sym, qty, new_client_id())
        except ccxt.InsufficientFunds:
            return await self._reconcile_position(sym)
        except Exception as exc:  # noqa: BLE001
            log.exception("sell %s", sym)
            self.notify(f"❌ Ошибка продажи {sym}: {exc}. Повтор на следующем тике.")
            return False
        if fill.filled_base <= 0:
            self.notify(f"❌ Продажа {sym} не исполнена ({fill.status}).")
            return False
        proceeds = fill.quote_amount - fill.fee_quote - fill.fee_base * fill.avg_price
        self._finalize_close(sym, fill.avg_price, proceeds, fill.fee_usdt, reason, kind)
        return True

    def _finalize_close(self, sym: str, exit_price: float, proceeds_net: float, exit_fee: float,
                        reason: str, kind: str = "other") -> None:
        st = self.state
        assert st is not None
        pos = st.positions[sym]
        pnl = self.engine.record_close(st, sym, proceeds_net, exit_fee)
        pct = pnl / pos.cost_usdt * 100 if pos.cost_usdt else 0.0
        if pos.trade_id:
            self.db.update_trade(pos.trade_id, status="closed", exit_ts=time.time(), exit_price=exit_price,
                                 proceeds_usdt=proceeds_net, exit_fee_usdt=exit_fee, exit_reason=reason,
                                 pnl_usdt=pnl, pnl_pct=pct, exit_kind=kind)
        self.persist()
        snap = self.engine.pnl(st, self.market.prices())
        icon = "✅" if pnl > 0 else "🔴"
        self.notify(f"{icon} ВЫХОД {sym}: {reason}\nЦена {exit_price:.6g} (вход {pos.entry_price:.6g}), "
                    f"PnL {pnl:+.4f} USDT ({pct:+.2f}%), комиссия выхода {exit_fee:.4f}\n"
                    f"Сессия #{st.session_id}: PnL {snap.total:+.4f} USDT, комиссии всего {st.fees_paid:.4f}")

    async def _reconcile_position(self, sym: str) -> bool:
        """Compare an open position with the real exchange balance."""
        st = self.state
        if st is None or sym not in st.positions:
            return True
        pos = st.positions[sym]
        total = await self.ex.base_total(sym)
        if total >= pos.qty * 0.95:
            if self.exchange_sl_tp and pos.algo_id:
                state = await self.ex.algo_state(pos.algo_id)
                if state in ("canceled", "order_failed"):
                    try:
                        pos.algo_id = await self.ex.place_oco(sym, pos.qty, pos.take_profit, pos.stop_loss)
                        pos.oco_stop = pos.stop_loss
                        self.persist()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("re-place OCO %s: %s", sym, exc)
                        pos.algo_id = None
            return False
        qty, quote, fee = await self.ex.sells_since(sym, int(pos.opened_at * 1000) - 1000)
        if qty > 0:
            ratio = min(1.0, pos.qty / qty)
            proceeds = (quote - fee) * ratio
            exit_price = quote / qty
            exit_fee = fee * ratio
        else:
            exit_price = self.market.price(sym) or pos.entry_price
            exit_fee = pos.qty * exit_price * self.engine.fee_rate
            proceeds = pos.qty * exit_price - exit_fee
        kind = "take_profit" if exit_price >= pos.take_profit * 0.99 else (
            "trailing_stop" if pos.stop_loss > pos.initial_stop > 0 else "stop_loss")
        self._finalize_close(sym, exit_price, proceeds, exit_fee, "закрыта на бирже (OCO SL/TP или вручную)", kind)
        return True

    async def _poll_algos(self) -> None:
        st = self.state
        if st is None:
            return
        for sym, pos in list(st.positions.items()):
            if not pos.algo_id:
                continue
            state = await self.ex.algo_state(pos.algo_id)
            if state in ("effective", "partially_effective", "canceled", "order_failed"):
                await self._reconcile_position(sym)

    # ================================================================== session end / report
    async def _finish_session(self) -> None:
        st = self.state
        if st is None:
            return
        if st.status != SessionStatus.ENDED:
            self.engine.finish(st, st.stop_reason or StopReason.MANUAL, time.time())
        self.persist()
        report = self.session_report(st)
        self.db.set_session_report(st.session_id, report)
        self.state = None
        self._daily_cache = None
        self.notify(report)

    def session_report(self, st: SessionState) -> str:
        trades = self.db.trades("session_id=? AND status='closed'", (st.session_id,))
        reason = STOP_REASON_RU.get(st.stop_reason, str(st.stop_reason)) if st.stop_reason else "—"
        dur = (st.ended_at or time.time()) - st.started_at
        lines = [f"🏁 Сессия #{st.session_id} завершена: {reason}",
                 f"Длительность {fmt_dur(dur)}, бюджет {st.params.budget_usdt:.2f} USDT",
                 f"Сделок: {st.trades_closed} (побед {st.wins}, поражений {st.losses}, винрейт {st.winrate * 100:.0f}%)",
                 f"PnL (с комиссиями): {st.realized_pnl:+.4f} USDT ({st.realized_pnl / st.params.budget_usdt * 100:+.2f}% бюджета)",
                 f"Комиссии: {st.fees_paid:.4f} USDT, макс. просадка: {st.max_drawdown:.4f} USDT, пик PnL: {st.peak_pnl:+.4f}"]
        pnls = [float(t["pnl_usdt"] or 0) for t in trades]
        gains, losses = sum(x for x in pnls if x > 0), -sum(x for x in pnls if x <= 0)
        if pnls:
            pf = "∞" if losses == 0 else f"{gains / losses:.2f}"
            kinds: dict[str, int] = {}
            for t in trades:
                kinds[t["exit_kind"] or "other"] = kinds.get(t["exit_kind"] or "other", 0) + 1
            lines.append(f"Profit factor {pf}, ожидание/сделку {sum(pnls) / len(pnls):+.4f}; выходы: "
                         + ", ".join(f"{k}={v}" for k, v in kinds.items()))
        for t in trades:
            lines.append(f"• {t['symbol']} {t['entry_price']:.6g}→{t['exit_price']:.6g} "
                         f"{t['pnl_usdt']:+.4f} ({t['exit_reason']})")
        return "\n".join(lines)

    # ================================================================== restore / reconcile
    async def restore(self) -> None:
        """After restart / wake-up: restore the active session and sync with the exchange.
        Never starts a new session automatically."""
        if self.ex.live and not self.live_confirmed:
            self.request_live_confirmation()
        row = self.db.active_session_row()
        if row is None:
            await self._resolve_pending(None)
            return
        if row["state_json"]:
            st = SessionState.from_dict(json.loads(row["state_json"]))
        else:
            params = SessionParams.from_dict(json.loads(row["params_json"]))
            st = SessionState(session_id=row["id"], params=params, started_at=row["started_at"],
                              ends_at=row["ends_at"])
        self.state = st
        st.manual_paused = self.paused
        if not self.ex.can_trade:
            self.notify(f"⚠️ Активная сессия #{st.session_id} найдена, но нет API-ключей — позиции не контролируются!")
            return
        async with self.lock:
            await self._resolve_pending(st)
            await self.reconcile()
        now = time.time()
        self.notify(f"♻️ Восстановлена сессия #{st.session_id} ({st.status.value}). "
                    f"Прошло {fmt_dur(now - st.started_at)}, осталось {fmt_dur(st.ends_at - now)}.\n"
                    f"Сделок {st.trades_opened}, реализ. PnL {st.realized_pnl:+.4f}, позиции: "
                    f"{', '.join(st.positions) or 'нет'}. Новая сессия автоматически не запускается.")
        self.persist()

    async def _resolve_pending(self, st: SessionState | None) -> None:
        """Orders that were sent right before a crash: find them by clOrdId."""
        if not self.ex.can_trade:
            return
        for t in self.db.trades("status='pending'"):
            try:
                fill = await self.ex.find_order(t["symbol"], t["client_order_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("pending trade %s: %s", t["id"], exc)
                continue
            if fill is None or fill.filled_base <= 0:
                self.db.update_trade(t["id"], status="failed", exit_reason="не найден/не исполнен после рестарта")
                continue
            if st is not None and st.session_id == t["session_id"] and t["symbol"] not in st.positions:
                await self._register_fill(st, t["id"], t["symbol"], fill, t["entry_reason"] or "")
            else:
                self.db.update_trade(t["id"], status="failed",
                                     exit_reason="исполнен вне активной сессии — проверьте вручную")
                self.notify(f"⚠️ Ордер {t['symbol']} ({t['client_order_id']}) исполнен, но сессия уже не активна. "
                            f"Проверьте баланс вручную.")

    async def reconcile(self) -> None:
        """Compare DB state with real balances (call under self.lock)."""
        st = self.state
        if st is None:
            return
        for sym in list(st.positions):
            try:
                await self._reconcile_position(sym)
            except Exception as exc:  # noqa: BLE001
                log.warning("reconcile %s: %s", sym, exc)
        try:
            free = await self.ex.free_usdt()
            if free + st.open_cost < min(st.params.budget_usdt, st.params.budget_usdt + st.realized_pnl) * 0.5:
                self.notify(f"⚠️ Свободных USDT ({free:.2f}) заметно меньше бюджета сессии.")
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile balance: %s", exc)

    async def on_wake(self) -> None:
        if self.state is None:
            return
        async with self.lock:
            await self.reconcile()
        self.persist()

    # ================================================================== texts
    def session_status_text(self) -> str:
        st = self.state
        if st is None:
            return "Нет активной сессии."
        d = self.engine.distances(st, self.market.prices(), time.time())
        snap = d["pnl"]
        p = st.params
        lines = [f"📊 Сессия #{st.session_id} [{st.status.value}]"
                 + (" ⏸ пауза" if st.manual_paused or (st.paused_until and st.paused_until > time.time()) else ""),
                 f"Осталось: {fmt_dur(d['time_left_sec'])}",
                 f"Бюджет {p.budget_usdt:.2f} USDT: в позициях {snap.open_cost:.2f}, доступно {d['budget_free']:.2f}",
                 f"PnL: {snap.total:+.4f} (реализ. {snap.realized:+.4f}, нереализ. {snap.unrealized:+.4f}), "
                 f"комиссии {snap.fees:.4f}",
                 f"До лимита убытка ({d['max_loss']:.2f}): {d['to_max_loss']:.4f} USDT",
                 (f"До цели ({d['target']:.2f}): {d['to_target']:.4f} USDT" if d["target"] is not None else "Цель: —")
                 + (" [profit_lock активен]" if st.profit_lock_active else ""),
                 f"Сделок осталось: {d['trades_left']}, убытков подряд: {st.consecutive_losses}/{p.max_consecutive_losses}",
                 f"Пик PnL {st.peak_pnl:+.4f}, макс. просадка {st.max_drawdown:.4f}"]
        return "\n".join(lines)

    def positions_text(self) -> str:
        st = self.state
        if st is None or not st.positions:
            return "Открытых позиций нет."
        lines = []
        for sym, pos in st.positions.items():
            price = self.market.price(sym) or pos.entry_price
            u = pos.unrealized(price, self.engine.fee_rate)
            lines.append(f"{sym}: {pos.qty:.8g} @ {pos.entry_price:.6g} → {price:.6g}, "
                         f"PnL {u:+.4f} USDT, SL {pos.stop_loss:.6g}, TP {pos.take_profit:.6g}"
                         f"{' [OCO]' if pos.algo_id else ''}, открыта "
                         f"{datetime.fromtimestamp(pos.opened_at, self.cfg.tz):%d.%m %H:%M}")
        return "\n".join(lines)

    def pnl_text(self) -> str:
        now = time.time()
        day = self._day_start(now)
        rows = {
            "Сегодня": self.db.realized_pnl_between(day, now + 1),
            "7 дней": self.db.realized_pnl_between(now - 7 * 86400, now + 1),
            "30 дней": self.db.realized_pnl_between(now - 30 * 86400, now + 1),
        }
        lines = ["💰 Реализованный PnL (с комиссиями):"] + [f"{k}: {v:+.4f} USDT" for k, v in rows.items()]
        if self.state is not None:
            snap = self.engine.pnl(self.state, self.market.prices())
            lines.append(f"Текущая сессия: {snap.total:+.4f} (нереализ. {snap.unrealized:+.4f})")
        return "\n".join(lines)

    def status_text(self) -> str:
        b = self.blockers()
        lines = [f"🤖 Режим: {self.mode}, trader: {'ВКЛ' if self.enabled else 'выкл'}"
                 f"{', пауза' if self.paused else ''}",
                 "Блокировки: " + ("; ".join(b) if b else "нет")]
        ages = [f"{s.split('/')[0]} {self.market.price(s) or 0:.6g} ({self.market.age(s):.0f}s)"
                for s in self.cfg.symbols]
        lines.append("Цены: " + ", ".join(ages))
        lines.append(f"Сессия: #{self.state.session_id} {self.state.status.value}" if self.state else "Сессия: нет")
        return "\n".join(lines)

    def next_schedule_hint(self) -> str:
        sc = self.cfg["schedule"]
        if not sc.get("enabled"):
            return "Расписание выключено."
        return f"Расписание: {sc['start_time']} ({', '.join(sc['days'])})"
