"""Dynamic pair selection for OKX spot /USDT (ideas from Freqtrade pairlists:
VolumePairList, SpreadFilter, VolatilityFilter, AgeFilter, PriceFilter/PrecisionFilter).

Pipeline: all active SPOT */USDT markets -> cheap filters on tickers (volume, price, spread,
stablecoins, leveraged tokens, blacklist) -> top-N by volume get order book + daily candles ->
depth, ATR%, listing age, manipulation heuristics -> rank by 24h quote volume -> max_pairs.

``evaluate_pair`` and ``select_pairs`` are pure (tested offline); ``PairSelector`` does the I/O.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

STABLECOINS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDP", "PYUSD", "USDE", "USDD", "USDG", "RLUSD", "USD1",
    "EURC", "EURT", "EUR", "GUSD", "BUSD", "LUSD", "FRAX", "CRVUSD", "GHO", "SUSD", "USTC", "UST",
    "XAUT", "PAXG",  # gold-backed: price ~ gold, not a crypto trend
}
LEVERAGED_PATTERNS = ("*UP/USDT", "*DOWN/USDT", "*3L/USDT", "*3S/USDT", "*5L/USDT", "*5S/USDT",
                      "*BULL/USDT", "*BEAR/USDT")


@dataclass
class PairlistConfig:
    enabled: bool = False
    refresh_hours: float = 6.0
    max_pairs: int = 4
    candidates: int = 25                 # how many top-volume pairs get the expensive checks
    min_quote_volume_usdt: float = 20_000_000
    max_spread_pct: float = 0.10
    depth_band_pct: float = 1.0          # depth is measured within ±band of mid price
    min_depth_usdt: float = 50_000       # on EACH side within the band
    atr_period: int = 14                 # on daily candles
    min_atr_pct: float = 2.0             # too quiet: TP +4% is rarely reached
    max_atr_pct: float = 12.0            # too wild for a 2% stop-loss
    min_listing_days: int = 90
    min_price: float = 0.0
    max_change_24h_pct: float = 25.0     # pumped/dumped right now -> skip
    max_volume_spike: float = 4.0        # 24h volume / median daily volume (30d)
    max_daily_range_pct: float = 35.0    # any single day in the last 30 with a bigger range -> skip
    whitelist: list[str] = field(default_factory=list)   # always considered first (still safety-checked)
    blacklist: list[str] = field(default_factory=list)   # wildcards allowed, e.g. "*DOG*/USDT"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PairlistConfig":
        d = dict(d or {})
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Неизвестные параметры pairlist: {', '.join(sorted(unknown))}")
        c = cls()
        for k, v in d.items():
            cur = getattr(c, k)
            setattr(c, k, [str(x).upper() for x in v] if isinstance(cur, list) else type(cur)(v))
        if not 1 <= c.max_pairs <= 10:
            raise ValueError("pairlist.max_pairs должен быть в [1; 10]")
        return c


@dataclass
class PairMetrics:
    symbol: str
    price: float = 0.0
    quote_volume: float = 0.0
    spread_pct: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    atr_pct: float | None = None
    age_days: float | None = None
    change_24h_pct: float | None = None
    volume_spike: float | None = None
    max_daily_range_pct: float | None = None
    min_order_usdt: float | None = None
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def short(self) -> str:
        def f(v: float | None, fmt: str) -> str:
            return "—" if v is None else format(v, fmt)
        return (f"{self.symbol}: объём {self.quote_volume / 1e6:.1f}M$, спред {f(self.spread_pct, '.4f')}%, "
                f"глубина ±1% {f((self.bid_depth or 0) / 1e3, '.0f')}k/{f((self.ask_depth or 0) / 1e3, '.0f')}k$, "
                f"ATR {f(self.atr_pct, '.1f')}%, возраст {f(self.age_days, '.0f')}д, 24ч {f(self.change_24h_pct, '+.1f')}%")


def is_blacklisted(symbol: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(symbol.upper(), p.upper()) for p in patterns)


def atr_pct(daily: list[list[float]], period: int) -> float | None:
    """Wilder ATR on daily candles as % of the last close."""
    if len(daily) < period + 1:
        return None
    trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4])) for p, c in zip(daily, daily[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr / daily[-1][4] * 100 if daily[-1][4] else None


def book_depth(book: dict[str, Any], band_pct: float) -> tuple[float, float, float | None]:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return 0.0, 0.0, None
    bb, ba = float(bids[0][0]), float(asks[0][0])
    mid = (bb + ba) / 2
    lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    bid_d = sum(float(p) * float(q) for p, q, *_ in bids if float(p) >= lo)
    ask_d = sum(float(p) * float(q) for p, q, *_ in asks if float(p) <= hi)
    return bid_d, ask_d, (ba - bb) / mid * 100


def prefilter(symbol: str, ticker: dict[str, Any], cfg: PairlistConfig) -> list[str]:
    """Cheap checks on the ticker only. Returns rejection reasons (empty = OK)."""
    reasons = []
    base = symbol.split("/")[0]
    if base in STABLECOINS:
        reasons.append("стейблкоин")
    if is_blacklisted(symbol, list(LEVERAGED_PATTERNS)):
        reasons.append("плечевой токен")
    if is_blacklisted(symbol, cfg.blacklist):
        reasons.append("blacklist")
    last = float(ticker.get("last") or 0)
    if last <= 0 or last < cfg.min_price:
        reasons.append(f"цена {last:g} < {cfg.min_price:g}")
    qv = float(ticker.get("quoteVolume") or 0)
    if qv < cfg.min_quote_volume_usdt:
        reasons.append(f"объём {qv / 1e6:.1f}M$ < {cfg.min_quote_volume_usdt / 1e6:.0f}M$")
    return reasons


def evaluate_pair(symbol: str, ticker: dict[str, Any], market: dict[str, Any], book: dict[str, Any],
                  daily: list[list[float]], cfg: PairlistConfig, now: float,
                  position_usdt: float) -> PairMetrics:
    m = PairMetrics(symbol=symbol, price=float(ticker.get("last") or 0),
                    quote_volume=float(ticker.get("quoteVolume") or 0))
    m.reasons = prefilter(symbol, ticker, cfg)
    open_ = float(ticker.get("open") or 0)
    if open_ > 0 and m.price:
        m.change_24h_pct = (m.price / open_ - 1) * 100
    m.bid_depth, m.ask_depth, m.spread_pct = book_depth(book, cfg.depth_band_pct)
    if m.spread_pct is None:
        m.reasons.append("нет стакана")
    elif m.spread_pct > cfg.max_spread_pct:
        m.reasons.append(f"спред {m.spread_pct:.3f}% > {cfg.max_spread_pct}%")
    if (m.bid_depth or 0) < cfg.min_depth_usdt or (m.ask_depth or 0) < cfg.min_depth_usdt:
        m.reasons.append(f"глубина ±{cfg.depth_band_pct:g}% < {cfg.min_depth_usdt / 1e3:.0f}k$")
    m.atr_pct = atr_pct(daily, cfg.atr_period)
    if m.atr_pct is None:
        m.reasons.append("мало дневных свечей для ATR")
    elif not cfg.min_atr_pct <= m.atr_pct <= cfg.max_atr_pct:
        m.reasons.append(f"ATR {m.atr_pct:.1f}% вне [{cfg.min_atr_pct:g}; {cfg.max_atr_pct:g}]%")
    # listing age: OKX instrument listTime (ms); fallback = number of daily candles
    info = market.get("info") or {}
    lt = info.get("listTime")
    if lt and str(lt).isdigit() and int(lt) > 0:
        m.age_days = (now * 1000 - int(lt)) / 86_400_000
    elif daily:
        m.age_days = (now * 1000 - daily[0][0]) / 86_400_000
    if m.age_days is None or m.age_days < cfg.min_listing_days:
        m.reasons.append(f"листинг {m.age_days or 0:.0f}д < {cfg.min_listing_days}д")
    # stablecoin heuristic for unknown tickers
    if m.atr_pct is not None and m.atr_pct < 0.5 and 0.97 <= m.price <= 1.03 and "стейблкоин" not in m.reasons:
        m.reasons.append("похоже на стейблкоин")
    # manipulation heuristics
    if m.change_24h_pct is not None and abs(m.change_24h_pct) > cfg.max_change_24h_pct:
        m.reasons.append(f"изменение 24ч {m.change_24h_pct:+.1f}% (памп/дамп)")
    if len(daily) >= 8:
        vols = [c[5] * c[4] for c in daily[:-1][-30:] if c[5]]   # approx. quote volume per day
        med = statistics.median(vols) if vols else 0
        if med > 0:
            m.volume_spike = m.quote_volume / med
            if m.volume_spike > cfg.max_volume_spike:
                m.reasons.append(f"всплеск объёма x{m.volume_spike:.1f} к медиане 30д")
        ranges = [(c[2] - c[3]) / c[3] * 100 for c in daily[-30:] if c[3] > 0]
        m.max_daily_range_pct = max(ranges) if ranges else None
        if m.max_daily_range_pct is not None and m.max_daily_range_pct > cfg.max_daily_range_pct:
            m.reasons.append(f"дневной диапазон до {m.max_daily_range_pct:.0f}% за 30д")
    # precision / min order (Freqtrade PrecisionFilter idea): the position must be tradable
    lim = market.get("limits") or {}
    min_amt = float((lim.get("amount") or {}).get("min") or 0)
    min_cost = float((lim.get("cost") or {}).get("min") or 0)
    m.min_order_usdt = max(min_amt * m.price, min_cost)
    if m.min_order_usdt > position_usdt * 0.5:
        m.reasons.append(f"мин. ордер {m.min_order_usdt:.2f}$ слишком велик для позиции {position_usdt:.2f}$")
    m.passed = not m.reasons
    return m


def select_pairs(metrics: list[PairMetrics], cfg: PairlistConfig) -> list[str]:
    """Whitelisted pairs first (only if they pass), then the most liquid ones."""
    ok = {m.symbol: m for m in metrics if m.passed}
    chosen = [s for s in cfg.whitelist if s in ok]
    for m in sorted(ok.values(), key=lambda x: x.quote_volume, reverse=True):
        if m.symbol not in chosen:
            chosen.append(m.symbol)
    return chosen[:cfg.max_pairs]


class PairSelector:
    """Fetches data from OKX (public endpoints, ccxt async client) and applies the filters."""

    def __init__(self, cfg: PairlistConfig, public_exchange: Any, position_usdt: float):
        self.cfg = cfg
        self.ex = public_exchange
        self.position_usdt = position_usdt

    async def evaluate(self) -> list[PairMetrics]:
        if not self.ex.markets:
            await self.ex.load_markets()
        markets = {s: m for s, m in self.ex.markets.items()
                   if m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)}
        tickers = await self.ex.fetch_tickers(params={"instType": "SPOT"})
        now = time.time()
        out: list[PairMetrics] = []
        pre_ok: list[str] = []
        for s in markets:
            t = tickers.get(s)
            if not t:
                continue
            reasons = prefilter(s, t, self.cfg)
            if reasons and s not in self.cfg.whitelist:
                out.append(PairMetrics(symbol=s, price=float(t.get("last") or 0),
                                       quote_volume=float(t.get("quoteVolume") or 0), reasons=reasons))
            else:
                pre_ok.append(s)
        pre_ok.sort(key=lambda s: float(tickers[s].get("quoteVolume") or 0), reverse=True)
        deep = [s for s in self.cfg.whitelist if s in pre_ok]
        deep += [s for s in pre_ok if s not in deep][:self.cfg.candidates]
        for s in deep:
            try:
                book = await self.ex.fetch_order_book(s, limit=200)
                daily = await self.ex.fetch_ohlcv(s, "1d", limit=max(40, self.cfg.atr_period + 10))
            except Exception as exc:  # noqa: BLE001
                log.warning("pairlist %s: %s", s, exc)
                out.append(PairMetrics(symbol=s, reasons=[f"ошибка данных: {exc}"]))
                continue
            out.append(evaluate_pair(s, tickers[s], markets[s], book, daily, self.cfg, now, self.position_usdt))
            await asyncio.sleep(0.1)
        return out


class PairlistService:
    """Keeps the current trading pairs: refresh every ``refresh_hours``, persist in kv,
    resubscribe the collector, notify about changes."""

    KV = "pairlist"

    def __init__(self, cfg: PairlistConfig, db: Any, selector: PairSelector, notify: Any,
                 fallback: list[str], on_change: Any = None):
        self.cfg = cfg
        self.db = db
        self.selector = selector
        self.notify = notify
        self.fallback = list(fallback)
        self.on_change = on_change
        saved = db.kv_get(self.KV) or {}
        self.pairs: list[str] = list(saved.get("pairs") or [])
        self.updated_at: float = float(saved.get("ts") or 0)
        self.metrics: list[dict[str, Any]] = list(saved.get("metrics") or [])

    def symbols(self) -> list[str]:
        return list(self.pairs) if (self.cfg.enabled and self.pairs) else list(self.fallback)

    async def refresh(self) -> list[str]:
        metrics = await self.selector.evaluate()
        pairs = select_pairs(metrics, self.cfg)
        old = self.symbols()
        self.metrics = [m.to_dict() for m in sorted(metrics, key=lambda m: m.quote_volume, reverse=True)
                        if m.passed or m.quote_volume >= self.cfg.min_quote_volume_usdt][:40]
        self.updated_at = time.time()
        if not pairs:
            self.notify("⚠️ Отбор пар: ни одна пара не прошла фильтры — оставляю прежний список.")
            pairs = old
        self.pairs = pairs
        self.db.kv_set(self.KV, {"ts": self.updated_at, "pairs": pairs, "metrics": self.metrics})
        if set(pairs) != set(old):
            by = {m["symbol"]: m for m in self.metrics}
            lines = [PairMetrics(**by[s]).short() if s in by else s for s in pairs]
            self.notify("🔄 Новый список пар для торговли:\n" + "\n".join(lines))
            if self.on_change is not None:
                await self.on_change(pairs)
        return pairs

    async def run(self) -> None:
        if not self.cfg.enabled:
            return
        while True:
            if time.time() - self.updated_at >= self.cfg.refresh_hours * 3600 or not self.pairs:
                try:
                    await self.refresh()
                except Exception as exc:  # noqa: BLE001
                    log.warning("pairlist refresh: %s", exc)
                    await asyncio.sleep(300)
                    continue
            await asyncio.sleep(300)

    def text(self) -> str:
        if not self.cfg.enabled:
            return "Динамический отбор пар выключен (pairlist.enabled). Торгуемые пары: " + ", ".join(self.fallback)
        from datetime import datetime
        when = datetime.fromtimestamp(self.updated_at).strftime("%d.%m %H:%M") if self.updated_at else "—"
        by = {m["symbol"]: m for m in self.metrics}
        lines = [f"📋 Пары для торговли (обновлено {when}, каждые {self.cfg.refresh_hours:g}ч):"]
        lines += ["✅ " + (PairMetrics(**by[s]).short() if s in by else s) for s in self.pairs]
        rejected = [m for m in self.metrics if not m["passed"]][:8]
        if rejected:
            lines.append("\nОтклонены (топ по объёму):")
            lines += [f"✖ {m['symbol']}: {'; '.join(m['reasons'])}" for m in rejected]
        return "\n".join(lines)
