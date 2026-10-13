"""Application wiring and lifecycle."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .alerts import AlertEngine
from .analytics import Analytics
from .collector import Collector, MarketState
from .config import Config
from .db import Database
from .exchange import Exchange
from .health import Health
from .events import EventLog
from .pairlist import PairlistConfig, PairlistService, PairSelector
from .scheduler import Scheduler
from .trader import Trader

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.path("db"))
        self.events = EventLog(self.db)
        self.health = Health(cfg.path("heartbeat_file"),
                             float(cfg["health"]["silent_alert_minutes"]) * 60, self.events.send)
        self.ex = Exchange(cfg)
        self.market = MarketState()
        self.collector = Collector(cfg, self.db, self.ex, self.market, self.health)
        self.alerts = AlertEngine(cfg, self.db, self.market, self.events.send)
        self.analytics = Analytics(cfg, self.db, self.market, self.events.send)
        self.trader = Trader(cfg, self.db, self.ex, self.market, self.health, self.events.send)
        self.scheduler = Scheduler(cfg, self.db, self.analytics, self.trader)
        pl_cfg = PairlistConfig.from_dict(cfg.raw.get("pairlist"))
        position_usdt = cfg.session_defaults.budget_usdt * cfg.session_defaults.position_size_pct / 100
        self.pairlist = PairlistService(pl_cfg, self.db, PairSelector(pl_cfg, self.ex.public, position_usdt),
                                        self.events.send, cfg.symbols, on_change=self._on_pairs_changed)
        self.trader.trading_symbols = self.pairlist.symbols
        self.collector.symbols = self._watch_symbols()
        self.collector.on_tick.append(self.alerts.on_tick)
        self.collector.on_candle_closed.append(self.alerts.on_candle_closed)
        self.collector.on_candle_closed.append(self.trader.on_candle_closed)
        from .web import WebUI
        self.web = WebUI(self)
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------------ helpers
    def _watch_symbols(self) -> list[str]:
        """Monitoring list + current trading pairs + symbols with open positions."""
        held = list(self.trader.state.positions) if self.trader.state else []
        btc = ["BTC/USDT"] if self.trader.strategy.btc_regime else []  # needed by the BTC regime filter
        return list(dict.fromkeys([*self.cfg.symbols, *btc, *self.pairlist.symbols(), *held]))

    async def _on_pairs_changed(self, pairs: list[str]) -> None:
        await self.collector.set_symbols(self._watch_symbols())

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[Any]]) -> None:
        """Keep a long-running coroutine alive: restart with backoff on unexpected errors."""
        backoff = 2.0
        while not self.stop_event.is_set():
            started = time.time()
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Задача %s упала", name)
                self.health.error(name, str(exc))
                if time.time() - started > 300:
                    backoff = 2.0
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _retry_forever(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        delay = 5.0
        while not self.stop_event.is_set():
            try:
                await fn()
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: %s — повтор через %.0fs", name, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _sleep_watcher(self) -> None:
        """Detect that the PC was asleep (event loop frozen) and resync."""
        last = time.time()
        while True:
            await asyncio.sleep(5)
            now = time.time()
            gap = now - last
            last = now
            if gap > 60:
                log.warning("Пауза %.0f сек — вероятно, сон/гибернация", gap)
                self.events.send(f"😴 Компьютер «спал» ~{gap / 60:.0f} мин. Переподключаюсь, сверяю позиции.")
                await self.collector.force_reconnect()
                try:
                    await self.trader.on_wake()
                except Exception:  # noqa: BLE001
                    log.exception("on_wake")

    async def _shutdown_watcher(self) -> None:
        f = self.cfg.path("shutdown_file")
        while True:
            if f.exists():
                # the file is removed by run_forever.py / start.bat, so the supervisor also sees it
                log.info("Найден %s — штатная остановка", f.name)
                self.stop_event.set()
                return
            await asyncio.sleep(2)

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        mode = self.ex.mode
        log.info("Запуск: режим %s, символы %s", mode, ", ".join(self.cfg.symbols))
        def spawn(name: str, fn: Callable[[], Awaitable[Any]]) -> None:
            self.tasks.append(asyncio.create_task(self._supervise(name, fn), name=name))

        # the local UI comes up first, so it shows what the bot is doing while it connects
        await self.web.start()
        await self._retry_forever("load_markets", self.ex.load_markets)

        spawn("health", lambda: self.health.run(float(self.cfg["health"]["check_interval_sec"])))
        spawn("collector", self.collector.run)
        spawn("sleep_watcher", self._sleep_watcher)
        spawn("shutdown_watcher", self._shutdown_watcher)

        # wait for history + first ticks before restoring the trading state
        try:
            await asyncio.wait_for(self.collector.backfilled_once.wait(), timeout=300)
        except asyncio.TimeoutError:
            log.warning("Backfill не завершился за 5 мин — продолжаю")
        for _ in range(30):
            if all(self.market.fresh(s, 60) for s in self.cfg.symbols):
                break
            await asyncio.sleep(1)
        await self._retry_forever("restore", self.trader.restore)

        # restored positions may be on pairs that are no longer in the list -> keep them subscribed
        await self.collector.set_symbols(self._watch_symbols())
        spawn("pairlist", self.pairlist.run)
        spawn("trader", self.trader.run)
        spawn("scheduler", self.scheduler.run)
        b = self.trader.blockers()
        self.events.send(f"🚀 Бот запущен [{mode}]. Мониторинг: {', '.join(s.split('/')[0] for s in self.cfg.symbols)}. "
                         f"Trader: {'ВКЛ' if self.trader.enabled else 'выкл'}"
                         + (f" ({'; '.join(b)})" if b and self.trader.enabled else "") + f". Интерфейс: {self.web.url}")
        await self.stop_event.wait()

    async def shutdown(self) -> None:
        log.info("Остановка...")
        self.stop_event.set()
        try:
            self.trader.persist()
        except Exception:  # noqa: BLE001
            log.exception("persist on shutdown")
        self.events.send("⏏️ Бот остановлен штатно." + (
            " Активная сессия будет восстановлена при следующем запуске." if self.trader.state else ""))
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.web.stop()
        await self.ex.close()
        self.db.close()
        log.info("Остановлено.")
