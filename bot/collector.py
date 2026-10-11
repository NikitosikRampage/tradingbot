"""Market data collector: OKX WebSocket (tickers + candles), auto-reconnect, REST backfill."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets

from .config import Config
from .db import Database
from .exchange import Exchange, inst_id, symbol_from_inst
from .health import Health
from .indicators import TF_SECONDS

log = logging.getLogger(__name__)

WS_CHANNEL = {"1m": "candle1m", "5m": "candle5m", "15m": "candle15m", "1h": "candle1H", "4h": "candle4H"}
CHANNEL_TF = {v: k for k, v in WS_CHANNEL.items()}

TickCallback = Callable[[str, dict[str, Any]], None]
CandleCallback = Callable[[str, str, list[float]], Awaitable[None] | None]


@dataclass
class MarketState:
    """Latest tickers in memory. ``updated`` = local receive time (epoch sec)."""
    tickers: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated: dict[str, float] = field(default_factory=dict)
    tick_event: asyncio.Event = field(default_factory=asyncio.Event)

    def price(self, symbol: str) -> float | None:
        t = self.tickers.get(symbol)
        return t.get("last") if t else None

    def prices(self) -> dict[str, float]:
        return {s: t["last"] for s, t in self.tickers.items() if t.get("last")}

    def age(self, symbol: str) -> float:
        t = self.updated.get(symbol)
        return time.time() - t if t else float("inf")

    def fresh(self, symbol: str, max_age: float) -> bool:
        return self.age(symbol) <= max_age

    def update(self, symbol: str, data: dict[str, Any]) -> None:
        self.tickers[symbol] = data
        self.updated[symbol] = time.time()
        self.tick_event.set()


class Collector:
    def __init__(self, cfg: Config, db: Database, ex: Exchange, market: MarketState, health: Health):
        self.cfg = cfg
        self.db = db
        self.ex = ex
        self.market = market
        self.health = health
        c = cfg["collector"]
        self.timeframes: list[str] = list(c["timeframes"])
        self.history: dict[str, int] = dict(c["history"])
        self.ping_interval = float(c["ping_interval_sec"])
        self.symbols = list(cfg.symbols)
        self.on_tick: list[TickCallback] = []
        self.on_candle_closed: list[CandleCallback] = []
        self._forming: dict[tuple[str, str], list[float]] = {}
        self._last_closed: dict[tuple[str, str], int] = {}
        self._backfill_lock = asyncio.Lock()
        self._sockets: dict[str, Any] = {}
        self.backfilled_once = asyncio.Event()

    # ------------------------------------------------------------------ public
    def _ticker_args(self) -> list[dict[str, str]]:
        return [{"channel": "tickers", "instId": inst_id(s)} for s in self.symbols]

    def _candle_args(self) -> list[dict[str, str]]:
        return [{"channel": WS_CHANNEL[tf], "instId": inst_id(s)}
                for s in self.symbols for tf in self.timeframes]

    async def run(self) -> None:
        await asyncio.gather(
            self._ws_loop("public", self.cfg["exchange"]["ws_public"], self._ticker_args),
            self._ws_loop("business", self.cfg["exchange"]["ws_business"], self._candle_args),
            self._maintenance(),
        )

    async def set_symbols(self, symbols: list[str]) -> None:
        """Change the subscribed symbols (dynamic pairlist): reconnect -> resubscribe + backfill."""
        new = list(dict.fromkeys(symbols))
        if set(new) == set(self.symbols):
            return
        log.info("Подписки: %s -> %s", ",".join(self.symbols), ",".join(new))
        self.symbols = new
        await self.force_reconnect()

    async def force_reconnect(self) -> None:
        """Called after the computer wakes up from sleep."""
        for ws in list(self._sockets.values()):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def backfill_all(self) -> None:
        async with self._backfill_lock:
            now_ms = int(time.time() * 1000)
            for sym in self.symbols:
                for tf in self.timeframes:
                    try:
                        await self._backfill(sym, tf, now_ms)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("backfill %s %s: %s", sym, tf, exc)
                        self.health.error("collector", f"backfill {sym} {tf}: {exc}")
            self.backfilled_once.set()

    # ------------------------------------------------------------------ internals
    async def _backfill(self, sym: str, tf: str, now_ms: int) -> None:
        step = TF_SECONDS[tf] * 1000
        last = self.db.last_candle_ts(sym, tf)
        since = last + step if last else now_ms - self.history.get(tf, 500) * step
        if since > now_ms - step:
            return
        rows = await self.ex.fetch_ohlcv_range(sym, tf, since, now_ms)
        closed = [r for r in rows if r[0] + step <= now_ms]
        forming = [r for r in rows if r[0] + step > now_ms]
        self.db.upsert_candles(sym, tf, closed, confirmed=True)
        if forming:
            self.db.upsert_candles(sym, tf, forming, confirmed=False)
        if closed:
            self._last_closed[(sym, tf)] = int(closed[-1][0])
            log.info("backfill %s %s: %d свечей", sym, tf, len(closed))

    async def _ws_loop(self, name: str, url: str, args_fn: Callable[[], list[dict[str, str]]]) -> None:
        backoff = 1.0
        while True:
            args = args_fn()
            try:
                async with websockets.connect(url, ping_interval=None, open_timeout=20,
                                              close_timeout=5, max_size=2 ** 22) as ws:
                    self._sockets[name] = ws
                    # OKX limits subscribe payload size -> chunk
                    for i in range(0, len(args), 20):
                        await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 20]}))
                    log.info("WS %s подключен (%d подписок)", name, len(args))
                    if name == "business":
                        asyncio.create_task(self.backfill_all())
                    backoff = 1.0
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        while True:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.ping_interval * 2 + 10)
                            self.health.beat(f"ws_{name}")
                            if raw == "pong":
                                continue
                            await self._handle(json.loads(raw))
                    finally:
                        pinger.cancel()
                        self._sockets.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("WS %s: %s: %s — переподключение через %.0fs", name, type(exc).__name__, exc, backoff)
                self.health.error(f"ws_{name}", str(exc))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _pinger(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            await ws.send("ping")

    async def _handle(self, msg: dict[str, Any]) -> None:
        if "event" in msg:
            if msg["event"] == "error":
                log.error("WS error: %s", msg)
            return
        arg = msg.get("arg") or {}
        ch = arg.get("channel", "")
        data = msg.get("data") or []
        if ch == "tickers":
            for d in data:
                sym = symbol_from_inst(d["instId"])
                t = {
                    "ts": int(d.get("ts") or 0), "last": float(d["last"]),
                    "bid": _f(d.get("bidPx")), "ask": _f(d.get("askPx")),
                    "high24": _f(d.get("high24h")), "low24": _f(d.get("low24h")),
                    "open24": _f(d.get("open24h")), "vol24": _f(d.get("volCcy24h")),
                }
                self.market.update(sym, t)
                self.health.beat("ticks")
                for cb in self.on_tick:
                    try:
                        cb(sym, t)
                    except Exception:  # noqa: BLE001
                        log.exception("on_tick callback")
        elif ch in CHANNEL_TF:
            tf = CHANNEL_TF[ch]
            sym = symbol_from_inst(arg["instId"])
            for d in data:
                row = [int(d[0]), float(d[1]), float(d[2]), float(d[3]), float(d[4]), float(d[5])]
                confirmed = str(d[8]) == "1" if len(d) > 8 else False
                await self._on_candle(sym, tf, row, confirmed)

    async def _on_candle(self, sym: str, tf: str, row: list[float], confirmed: bool) -> None:
        key = (sym, tf)
        prev = self._forming.get(key)
        # Safety net: the next candle started but the previous one never came with confirm=1.
        # Only for adjacent candles — after a long disconnect the REST backfill restores history.
        step = TF_SECONDS[tf] * 1000
        if (prev is not None and row[0] - prev[0] == step
                and self._last_closed.get(key, 0) < prev[0]):
            await self._close_candle(sym, tf, prev)
        if confirmed:
            if self._last_closed.get(key, 0) < row[0]:
                await self._close_candle(sym, tf, row)
        else:
            self._forming[key] = row
            self.db.upsert_candles(sym, tf, [row], confirmed=False)

    async def _close_candle(self, sym: str, tf: str, row: list[float]) -> None:
        self._last_closed[(sym, tf)] = int(row[0])
        self.db.upsert_candles(sym, tf, [row], confirmed=True)
        for cb in self.on_candle_closed:
            try:
                res = cb(sym, tf, row)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("on_candle_closed callback")

    async def _maintenance(self) -> None:
        flush = float(self.cfg["collector"]["ticker_flush_sec"])
        retention = self.cfg["collector"]["retention_days"]
        last_prune = 0.0
        while True:
            await asyncio.sleep(flush)
            try:
                if self.market.tickers:
                    self.db.save_tickers(self.market.tickers)
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    for tf, days in retention.items():
                        self.db.prune_candles(tf, int((time.time() - float(days) * 86400) * 1000))
            except Exception:  # noqa: BLE001
                log.exception("collector maintenance")


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
