"""OKX access through ccxt (async). Public client for market data, private for trading.

DEMO mode: ccxt ``set_sandbox_mode(True)`` adds the ``x-simulated-trading: 1`` header
(requires API keys created in OKX Demo Trading).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import ccxt.async_support as ccxt

from .config import Config
from .indicators import TF_SECONDS

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class Fill:
    order_id: str
    client_order_id: str
    filled_base: float     # gross base filled
    avg_price: float
    quote_amount: float    # gross USDT spent (buy) / received (sell)
    fee_base: float
    fee_quote: float
    status: str
    raw: dict[str, Any]

    @property
    def fee_usdt(self) -> float:
        return self.fee_quote + self.fee_base * self.avg_price


def inst_id(symbol: str) -> str:
    return symbol.replace("/", "-")


def symbol_from_inst(inst: str) -> str:
    return inst.replace("-", "/")


def new_client_id(prefix: str = "tb") -> str:
    # OKX clOrdId: alphanumeric, 1-32 chars
    return (prefix + uuid.uuid4().hex)[:32]


class Exchange:
    RETRYABLE = (ccxt.NetworkError,)  # includes RequestTimeout, RateLimitExceeded, DDoSProtection, ExchangeNotAvailable

    def __init__(self, cfg: Config):
        self.cfg = cfg
        timeout = int(cfg["exchange"]["request_timeout_ms"])
        self.public = ccxt.okx({"enableRateLimit": True, "timeout": timeout})
        s = cfg.secrets
        self.live = s.live_trading
        self.private: ccxt.okx | None = None
        if s.okx_key and s.okx_secret and s.okx_passphrase:
            self.private = ccxt.okx({"apiKey": s.okx_key, "secret": s.okx_secret,
                                     "password": s.okx_passphrase, "enableRateLimit": True,
                                     "timeout": timeout, "options": {"defaultType": "spot"}})
            if not self.live:
                self.private.set_sandbox_mode(True)
        self.markets_loaded = False

    @property
    def mode(self) -> str:
        return "LIVE" if self.live else "DEMO"

    @property
    def can_trade(self) -> bool:
        return self.private is not None

    async def close(self) -> None:
        for ex in (self.public, self.private):
            if ex is not None:
                try:
                    await ex.close()
                except Exception:  # noqa: BLE001
                    pass

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, retries: int = 4, **kw: Any) -> T:
        """Retry idempotent calls on network errors / rate limits with exponential backoff."""
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                return await fn(*args, **kw)
            except self.RETRYABLE as exc:
                if attempt >= retries:
                    raise
                if isinstance(exc, ccxt.RateLimitExceeded):
                    delay = max(delay, 2.0)
                log.warning("OKX %s: %s — повтор через %.0fs", getattr(fn, "__name__", "call"),
                            type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        raise RuntimeError("unreachable")

    async def load_markets(self) -> None:
        await self.call(self.public.load_markets)
        if self.private is not None:
            await self.call(self.private.load_markets)
        self.markets_loaded = True

    # ------------------------------------------------------------------ market data
    async def fetch_ohlcv_range(self, symbol: str, tf: str, since_ms: int,
                                until_ms: int | None = None) -> list[list[float]]:
        """Paginated history. Returns [ts, o, h, l, c, v] sorted ascending, may include the forming candle."""
        step = TF_SECONDS[tf] * 1000
        until_ms = until_ms or int(time.time() * 1000)
        out: dict[int, list[float]] = {}
        cursor = since_ms
        guard = 0
        while cursor <= until_ms and guard < 500:
            guard += 1
            rows = await self.call(self.public.fetch_ohlcv, symbol, tf, since=cursor, limit=100)
            rows = [r for r in rows if r[0] >= cursor]
            if not rows:
                break
            for r in rows:
                out[int(r[0])] = [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)]
            nxt = int(rows[-1][0]) + step
            if nxt <= cursor:
                break
            cursor = nxt
        return [out[k] for k in sorted(out)]

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        res = await self.call(self.public.fetch_tickers, symbols)
        return res

    # ------------------------------------------------------------------ account
    def _need_private(self) -> ccxt.okx:
        if self.private is None:
            raise RuntimeError("Нет API-ключей OKX в .env")
        return self.private

    async def balance(self) -> dict[str, Any]:
        return await self.call(self._need_private().fetch_balance)

    async def free_usdt(self) -> float:
        bal = await self.balance()
        return float((bal.get("free") or {}).get("USDT") or 0.0)

    def base_ccy(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        ex = self._need_private()
        return float(ex.amount_to_precision(symbol, amount))

    def price_to_precision(self, symbol: str, price: float) -> str:
        return self._need_private().price_to_precision(symbol, price)

    def min_amount(self, symbol: str) -> float:
        m = self._need_private().market(symbol)
        return float(((m.get("limits") or {}).get("amount") or {}).get("min") or 0.0)

    # ------------------------------------------------------------------ orders
    def _parse_fill(self, order: dict[str, Any], symbol: str) -> Fill:
        base = self.base_ccy(symbol)
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_base = fee_quote = 0.0
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            cost = abs(float(f["cost"]))
            if f.get("currency") == base:
                fee_base += cost
            else:
                fee_quote += cost
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or order.get("price") or 0.0)
        quote = float(order.get("cost") or filled * avg)
        return Fill(order_id=str(order.get("id")), client_order_id=str(order.get("clientOrderId") or ""),
                    filled_base=filled, avg_price=avg, quote_amount=quote, fee_base=fee_base,
                    fee_quote=fee_quote, status=str(order.get("status")), raw=order)

    async def _wait_fill(self, symbol: str, order_id: str | None, client_id: str,
                         timeout: float = 15.0) -> Fill:
        ex = self._need_private()
        deadline = time.time() + timeout
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                last = await self.call(ex.fetch_order, order_id, symbol,
                                       {} if order_id else {"clOrdId": client_id})
                if last.get("status") in ("closed", "canceled", "rejected", "expired"):
                    break
            except ccxt.OrderNotFound:
                pass
            await asyncio.sleep(1.0)
        if last is None:
            raise RuntimeError(f"Ордер {client_id} не найден")
        return self._parse_fill(last, symbol)

    async def find_order(self, symbol: str, client_id: str) -> Fill | None:
        try:
            o = await self.call(self._need_private().fetch_order, None, symbol, {"clOrdId": client_id})
            return self._parse_fill(o, symbol)
        except ccxt.OrderNotFound:
            return None

    async def market_buy_cost(self, symbol: str, cost_usdt: float, client_id: str) -> Fill:
        """Spot market buy for a given USDT amount. Safe against duplicate submission."""
        ex = self._need_private()
        cost = float(ex.cost_to_precision(symbol, cost_usdt))
        try:
            order = await ex.create_market_buy_order_with_cost(
                symbol, cost, params={"clientOrderId": client_id, "tdMode": "cash"})
            oid = order.get("id")
        except ccxt.NetworkError:
            # unknown outcome — never resend blindly, look the order up by clOrdId
            await asyncio.sleep(2)
            found = await self.find_order(symbol, client_id)
            if found is None:
                raise
            oid = found.order_id
        return await self._wait_fill(symbol, oid, client_id)

    async def market_sell(self, symbol: str, qty: float, client_id: str) -> Fill:
        ex = self._need_private()
        amount = self.amount_to_precision(symbol, qty)
        if amount <= 0:
            raise ValueError(f"Количество {qty} меньше минимального шага")
        try:
            order = await ex.create_order(symbol, "market", "sell", amount,
                                          params={"clientOrderId": client_id, "tdMode": "cash"})
            oid = order.get("id")
        except ccxt.NetworkError:
            await asyncio.sleep(2)
            found = await self.find_order(symbol, client_id)
            if found is None:
                raise
            oid = found.order_id
        return await self._wait_fill(symbol, oid, client_id)

    async def base_total(self, symbol: str) -> float:
        bal = await self.balance()
        return float((bal.get("total") or {}).get(self.base_ccy(symbol)) or 0.0)

    async def base_free(self, symbol: str) -> float:
        bal = await self.balance()
        return float((bal.get("free") or {}).get(self.base_ccy(symbol)) or 0.0)

    # ------------------------------------------------------------------ exchange-side OCO (SL/TP)
    async def place_oco(self, symbol: str, qty: float, take_profit: float, stop_loss: float) -> str:
        ex = self._need_private()
        market = ex.market(symbol)
        req = {
            "instId": market["id"], "tdMode": "cash", "side": "sell", "ordType": "oco",
            "sz": ex.amount_to_precision(symbol, qty),
            "tpTriggerPx": ex.price_to_precision(symbol, take_profit), "tpOrdPx": "-1",
            "slTriggerPx": ex.price_to_precision(symbol, stop_loss), "slOrdPx": "-1",
        }
        res = await ex.private_post_trade_order_algo(req)
        data = (res.get("data") or [{}])[0]
        if str(data.get("sCode", "0")) != "0" or not data.get("algoId"):
            raise RuntimeError(f"OCO отклонён: {data.get('sCode')} {data.get('sMsg')}")
        return str(data["algoId"])

    async def cancel_algo(self, symbol: str, algo_id: str) -> None:
        ex = self._need_private()
        try:
            await self.call(ex.cancel_order, algo_id, symbol, {"trigger": True})
        except ccxt.ExchangeError as exc:
            log.warning("cancel algo %s: %s", algo_id, exc)

    async def algo_state(self, algo_id: str) -> str | None:
        ex = self._need_private()
        try:
            res = await self.call(ex.private_get_trade_order_algo, {"algoId": algo_id})
            data = res.get("data") or []
            return str(data[0].get("state")) if data else None
        except ccxt.ExchangeError as exc:
            log.warning("algo state %s: %s", algo_id, exc)
            return None

    async def sells_since(self, symbol: str, since_ms: int) -> tuple[float, float, float]:
        """Sum of (base qty, USDT received gross, fee USDT) of sell fills since ``since_ms``."""
        trades = await self.call(self._need_private().fetch_my_trades, symbol, since_ms, 100)
        qty = quote = fee = 0.0
        for t in trades:
            if t.get("side") != "sell":
                continue
            qty += float(t.get("amount") or 0)
            quote += float(t.get("cost") or 0)
            f = t.get("fee") or {}
            if f.get("cost") is not None:
                c = abs(float(f["cost"]))
                fee += c if f.get("currency") == "USDT" else c * float(t.get("price") or 0)
        return qty, quote, fee
