"""Alert rules -> Telegram, with per-rule/per-coin cooldown."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .collector import MarketState
from .config import Config
from .db import Database
from .indicators import ema, pct_change, rsi
from .marketdata import closes, closes_4h

log = logging.getLogger(__name__)

WINDOW_HOURS = {"24h": 24, "7d": 168}


class AlertEngine:
    def __init__(self, cfg: Config, db: Database, market: MarketState, notify: Callable[[str], None]):
        self.cfg = cfg
        self.a = cfg["alerts"]
        self.db = db
        self.market = market
        self.notify = notify
        self.last_fired: dict[str, float] = {}
        self._last_check: dict[str, float] = {}
        self.levels: dict[str, dict[str, tuple[float, float]]] = {}  # sym -> window -> (low, high)
        self.touch: dict[str, str | None] = {}  # f"{sym}:{window}:{high|low}" -> "touched"
        self.muted_until = 0.0
        self._restore_cooldowns()

    # ------------------------------------------------------------------ cooldown
    def _restore_cooldowns(self) -> None:
        for r in self.db.query("SELECT rule, symbol, MAX(ts) AS t FROM alerts_log GROUP BY rule, symbol"):
            self.last_fired[f"{r['rule']}|{r['symbol']}"] = float(r["t"])

    def _cooldown(self, rule: str) -> float:
        base = rule.split(":")[0]
        cd = self.a["cooldown_minutes"]
        return float(cd.get(base, cd.get("default", 60))) * 60.0

    def fire(self, rule: str, symbol: str, text: str) -> bool:
        key = f"{rule}|{symbol}"
        now = time.time()
        if now - self.last_fired.get(key, 0.0) < self._cooldown(rule):
            return False
        self.last_fired[key] = now
        self.db.log_alert(rule, symbol, text)
        if now >= self.muted_until:
            self.notify(text)
        return True

    # ------------------------------------------------------------------ hooks
    def on_tick(self, symbol: str, t: dict[str, Any]) -> None:
        if not self.a.get("enabled", True):
            return
        now = time.time()
        if now - self._last_check.get(symbol, 0.0) < 5.0:
            return
        self._last_check[symbol] = now
        price = t["last"]
        try:
            self.check_price_move(symbol, price)
            self.check_levels(symbol, price)
        except Exception:  # noqa: BLE001
            log.exception("alerts on_tick %s", symbol)

    async def on_candle_closed(self, symbol: str, tf: str, row: list[float]) -> None:
        if not self.a.get("enabled", True):
            return
        vs = self.a["volume_spike"]
        if vs.get("enabled") and tf == vs["timeframe"]:
            self.check_volume(symbol, tf)
        if tf == "1h":
            self.refresh_levels(symbol)
            if self.a["rsi"].get("enabled"):
                self.check_rsi(symbol)
            # a 4h candle closes when the 1h candle that ends on a 4h boundary closes
            if self.a["ema_cross"].get("enabled") and (int(row[0]) // 1000 + 3600) % 14400 == 0:
                self.check_ema_cross(symbol)

    # ------------------------------------------------------------------ rules
    def check_price_move(self, symbol: str, price: float) -> None:
        now_ms = int(time.time() * 1000)
        for rule in self.a.get("price_move") or []:
            minutes, thr = int(rule["minutes"]), float(rule["pct"])
            ref_ts = (now_ms - minutes * 60_000) // 60_000 * 60_000
            row = self.db.candle_at_or_after(symbol, "1m", ref_ts)
            if row is None or row[0] - ref_ts > 120_000:
                continue
            ref = row[1]  # open of the candle N minutes ago
            ch = pct_change(ref, price)
            if abs(ch) >= thr:
                arrow = "📈" if ch > 0 else "📉"
                self.fire(f"price_move:{minutes}m:{'up' if ch > 0 else 'down'}", symbol,
                          f"{arrow} {symbol} {ch:+.2f}% за {minutes} мин: {ref:.6g} → {price:.6g}")

    def refresh_levels(self, symbol: str) -> None:
        rows = self.db.candles(symbol, "1h", limit=168)
        if not rows:
            return
        lv = {}
        for w in self.a["levels"]["windows"]:
            sub = rows[-WINDOW_HOURS[w]:]
            lv[w] = (min(r[3] for r in sub), max(r[2] for r in sub))
        self.levels[symbol] = lv

    def check_levels(self, symbol: str, price: float) -> None:
        cfg = self.a["levels"]
        if not cfg.get("enabled"):
            return
        if symbol not in self.levels:
            self.refresh_levels(symbol)
        touch = float(cfg["touch_pct"]) / 100.0
        bounce = float(cfg["bounce_pct"]) / 100.0
        for w, (low, high) in self.levels.get(symbol, {}).items():
            # breakouts
            if price > high:
                self.fire(f"levels:{w}:breakout_up", symbol,
                          f"🚀 {symbol} пробил {w} high {high:.6g} (цена {price:.6g})")
            elif price < low:
                self.fire(f"levels:{w}:breakout_down", symbol,
                          f"🔻 {symbol} пробил {w} low {low:.6g} (цена {price:.6g})")
            # bounces: touch the level, then move away by bounce_pct without breaking it
            for side, lvl in (("high", high), ("low", low)):
                k = f"{symbol}:{w}:{side}"
                if abs(price - lvl) / lvl <= touch:
                    self.touch[k] = "touched"
                elif self.touch.get(k) == "touched":
                    if side == "high" and price < lvl * (1 - bounce):
                        self.touch[k] = None
                        self.fire(f"levels:{w}:reject_high", symbol,
                                  f"↩️ {symbol} отскок вниз от {w} high {lvl:.6g} (цена {price:.6g})")
                    elif side == "low" and price > lvl * (1 + bounce):
                        self.touch[k] = None
                        self.fire(f"levels:{w}:bounce_low", symbol,
                                  f"↪️ {symbol} отскок вверх от {w} low {lvl:.6g} (цена {price:.6g})")
                    elif (side == "high" and price > lvl) or (side == "low" and price < lvl):
                        self.touch[k] = None

    def check_rsi(self, symbol: str) -> None:
        c = self.a["rsi"]
        cl = closes(self.db, symbol, "1h", 300)
        r = rsi(cl, int(c["period"]))[-1] if cl else None
        if r is None:
            return
        if r > float(c["overbought"]):
            self.fire("rsi:overbought", symbol, f"🔥 {symbol} RSI{c['period']} 1h = {r:.1f} (> {c['overbought']})")
        elif r < float(c["oversold"]):
            self.fire("rsi:oversold", symbol, f"🧊 {symbol} RSI{c['period']} 1h = {r:.1f} (< {c['oversold']})")

    def check_volume(self, symbol: str, tf: str) -> None:
        c = self.a["volume_spike"]
        lb = int(c["lookback"])
        rows = self.db.candles(symbol, tf, limit=lb + 1)
        if len(rows) < lb + 1:
            return
        last = rows[-1]
        avg = sum(r[5] for r in rows[:-1]) / lb
        if avg > 0 and last[5] > float(c["multiplier"]) * avg:
            ch = pct_change(last[1], last[4])
            self.fire(f"volume_spike:{tf}", symbol,
                      f"📊 {symbol} всплеск объёма {tf}: x{last[5] / avg:.1f} от среднего за {lb} свечей "
                      f"(свеча {ch:+.2f}%)")

    def check_ema_cross(self, symbol: str) -> None:
        c = self.a["ema_cross"]
        fast, slow = int(c["fast"]), int(c["slow"])
        cl = closes_4h(self.db, symbol, slow + 60)
        if len(cl) < slow + 2:
            return
        ef, es = ema(cl, fast), ema(cl, slow)
        if None in (ef[-1], ef[-2], es[-1], es[-2]):
            return
        prev = ef[-2] - es[-2]  # type: ignore[operator]
        cur = ef[-1] - es[-1]  # type: ignore[operator]
        if prev <= 0 < cur:
            self.fire("ema_cross:golden", symbol, f"✨ {symbol} золотой крест 4h: EMA{fast} пересекла EMA{slow} снизу вверх")
        elif prev >= 0 > cur:
            self.fire("ema_cross:death", symbol, f"☠️ {symbol} крест смерти 4h: EMA{fast} пересекла EMA{slow} сверху вниз")
