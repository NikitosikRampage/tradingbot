"""Morning digest and weekly report."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any, Callable

from .collector import MarketState
from .config import Config
from .db import Database
from .indicators import correlation, ema, log_returns, pct_change, rsi, stdev
from .llm import LlmAnalyst
from .marketdata import closes_4h

log = logging.getLogger(__name__)


def _fmt(x: float | None, nd: int = 2, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


class Analytics:
    def __init__(self, cfg: Config, db: Database, market: MarketState, notify: Callable[[str], None]):
        self.cfg = cfg
        self.db = db
        self.market = market
        self.notify = notify
        self.llm = LlmAnalyst(cfg, db)

    # ------------------------------------------------------------------ metrics
    def symbol_stats(self, symbol: str, hours: int, corr_hours: int) -> dict[str, Any] | None:
        rows = self.db.candles(symbol, "1h", limit=max(hours, corr_hours, 168) + 1)
        if len(rows) < hours + 1:
            return None
        cl = [r[4] for r in rows]
        price = self.market.price(symbol) or cl[-1]
        win = rows[-hours:]
        rets = log_returns(cl[-hours - 1:])
        vol = stdev(rets) * math.sqrt(24) * 100  # daily realised vol, %
        r = rsi(cl, 14)[-1]
        c4 = closes_4h(self.db, symbol, 260)
        e50 = ema(c4, 50)[-1] if len(c4) >= 50 else None
        e200 = ema(c4, 200)[-1] if len(c4) >= 200 else None
        trend = None
        if e50 is not None and e200 is not None:
            trend = "вверх" if e50 > e200 else "вниз"
        # yesterday's pivot (last 24 closed hours)
        d = rows[-24:]
        h, l, c = max(x[2] for x in d), min(x[3] for x in d), d[-1][4]
        p = (h + l + c) / 3
        return {
            "symbol": symbol, "price": price,
            "change": pct_change(cl[-hours - 1], price),
            "vol": vol, "high": max(x[2] for x in win), "low": min(x[3] for x in win),
            "high7d": max(x[2] for x in rows[-168:]), "low7d": min(x[3] for x in rows[-168:]),
            "pivot": p, "r1": 2 * p - l, "s1": 2 * p - h,
            "rsi": r, "trend": trend,
            "returns": log_returns(cl[-corr_hours - 1:]),
        }

    def _btc_corr(self, stats: dict[str, dict[str, Any]]) -> None:
        btc = stats.get("BTC/USDT")
        for s, st in stats.items():
            st["corr"] = None if (btc is None or s == "BTC/USDT") else correlation(st["returns"], btc["returns"])

    def _trading_summary(self, since: float) -> str:
        rows = self.db.query(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl_usdt),0) pnl, COALESCE(SUM(entry_fee_usdt+exit_fee_usdt),0) fees, "
            "SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) wins FROM trades WHERE status='closed' AND exit_ts>=?",
            (since,))
        r = rows[0]
        sessions = self.db.sessions_since(since)
        if not r["n"] and not sessions:
            return ""
        wr = (r["wins"] or 0) / r["n"] * 100 if r["n"] else 0
        return (f"\n💼 Торговля: сессий {len(sessions)}, сделок {r['n']}, PnL {r['pnl']:+.2f} USDT "
                f"(комиссии {r['fees']:.2f}), винрейт {wr:.0f}%")

    # ------------------------------------------------------------------ reports
    def build_digest(self) -> str:
        stats = {s: st for s in self.cfg.symbols if (st := self.symbol_stats(s, 24, 168))}
        self._btc_corr(stats)
        now = datetime.now(self.cfg.tz)
        lines = [f"☀️ Утренний дайджест {now:%d.%m.%Y %H:%M}"]
        for st in stats.values():
            lines.append(
                f"\n{st['symbol']}: {st['price']:.6g} ({_fmt(st['change'], sign=True)}% за 24ч)\n"
                f"  волат. {_fmt(st['vol'])}%/д, RSI1h {_fmt(st['rsi'], 0)}, тренд 4h {st['trend'] or '—'}\n"
                f"  24ч: {st['low']:.6g}–{st['high']:.6g}; 7д: {st['low7d']:.6g}–{st['high7d']:.6g}\n"
                f"  pivot {st['pivot']:.6g}, R1 {st['r1']:.6g}, S1 {st['s1']:.6g}"
                + (f"; корр. с BTC (7д) {_fmt(st['corr'])}" if st.get("corr") is not None else ""))
        if not stats:
            lines.append("Недостаточно данных (идёт загрузка истории).")
        lines.append(self._trading_summary(time.time() - 86400))
        return "\n".join(x for x in lines if x)

    def build_weekly(self) -> str:
        stats = {s: st for s in self.cfg.symbols if (st := self.symbol_stats(s, 168, 168))}
        self._btc_corr(stats)
        now = datetime.now(self.cfg.tz)
        lines = [f"📅 Недельный отчёт {now:%d.%m.%Y}"]
        ranked = sorted(stats.values(), key=lambda s: s["change"], reverse=True)
        for st in ranked:
            lines.append(
                f"{st['symbol']}: {_fmt(st['change'], sign=True)}% за 7д, диапазон {st['low']:.6g}–{st['high']:.6g}, "
                f"волат. {_fmt(st['vol'])}%/д, тренд 4h {st['trend'] or '—'}"
                + (f", корр. BTC {_fmt(st['corr'])}" if st.get("corr") is not None else ""))
        if ranked:
            lines.append(f"\nЛучшая: {ranked[0]['symbol']}, худшая: {ranked[-1]['symbol']}")
        lines.append(self._trading_summary(time.time() - 7 * 86400))
        return "\n".join(x for x in lines if x)

    async def send_digest(self) -> None:
        text = self.build_digest()
        self.notify(text)
        await self._llm(text)

    async def send_weekly(self) -> None:
        text = self.build_weekly()
        self.notify(text)
        await self._llm(text)

    async def _llm(self, text: str) -> None:
        if not self.llm.enabled:
            return
        try:
            comment = await self.llm.summarize(text)
        except Exception:  # noqa: BLE001
            log.exception("LLM summary")
            return
        if comment:
            self.notify("🤖 Разбор Claude:\n" + comment)
