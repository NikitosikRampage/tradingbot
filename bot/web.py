"""Local web interface: dashboard, controls, analytics. Replaces the Telegram bot.

Security model (it controls real money on a live account):
* binds to 127.0.0.1 by default — not reachable from other machines;
* every /api request needs the per-run token embedded in the page (``X-Token`` header), so
  another website open in the same browser cannot drive the bot (it can't read the token);
* the Host header must be the local address (blocks DNS-rebinding);
* state-changing calls are POST with a JSON body.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .risk import STOP_REASON_RU, StopReason

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)
UI_DIR = Path(__file__).resolve().parent / "ui"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _f(x: Any, nd: int = 6) -> float | None:
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


class WebUI:
    def __init__(self, core: "App"):
        self.core = core
        ui = core.cfg.raw.get("ui") or {}
        host = str(ui.get("host", "127.0.0.1"))
        if host not in LOOPBACK:
            # the UI can stop trading and start sessions: never expose it to the network
            log.warning("ui.host=%s игнорируется: интерфейс доступен только с этого компьютера (127.0.0.1)", host)
            host = "127.0.0.1"
        self.host = host
        self.port = int(ui.get("port", 8765))
        self.token = secrets.token_urlsafe(24)
        self.runner: web.AppRunner | None = None
        self.started = time.time()
        self.url_file = core.cfg.path("db").parent / "ui_url.txt"
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    @property
    def url(self) -> str:
        return f"http://{'[::1]' if self.host == '::1' else self.host}:{self.port}"

    # ------------------------------------------------------------------ server
    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard])
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/trades", self.api_trades)
        r.add_get("/api/sessions", self.api_sessions)
        r.add_get("/api/analytics", self.api_analytics)
        r.add_get("/api/events", self.api_events)
        r.add_get("/api/candles", self.api_candles)
        r.add_get("/api/research", self.api_research)
        r.add_post("/api/session/start", self.post_session_start)
        r.add_post("/api/session/end", self.post_session_end)
        r.add_post("/api/session/set", self.post_session_set)
        r.add_post("/api/pause", self.post_pause)
        r.add_post("/api/stop", self.post_stop)
        r.add_post("/api/strategy", self.post_strategy)
        r.add_post("/api/trader", self.post_trader)
        r.add_post("/api/confirm_live", self.post_confirm_live)
        r.add_post("/api/pairs/refresh", self.post_pairs_refresh)
        r.add_post("/api/report", self.post_report)
        r.add_post("/api/mute", self.post_mute)
        return app

    async def start(self) -> None:
        try:
            self.runner = web.AppRunner(self.build_app(), access_log=None)
            await self.runner.setup()
            await web.TCPSite(self.runner, self.host, self.port).start()
            log.info("Интерфейс: %s", self.url)
            try:  # start.bat waits for this file and opens the browser
                self.url_file.parent.mkdir(parents=True, exist_ok=True)
                self.url_file.write_text(self.url, encoding="utf-8")
            except OSError:
                pass
        except OSError as exc:
            log.error("Интерфейс не запущен (%s): порт %s занят? Бот продолжает работать без него.", exc, self.port)
            self.core.events.send(f"⚠️ Интерфейс не запущен: порт {self.port} занят ({exc}).")

    async def stop(self) -> None:
        self.url_file.unlink(missing_ok=True)
        if self.runner is not None:
            await self.runner.cleanup()

    @web.middleware
    async def _guard(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.host not in self.allowed_hosts:
            return web.Response(status=403, text="forbidden host")
        if request.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("X-Token", ""), self.token):
                return web.json_response({"error": "Сессия интерфейса устарела — обновите страницу."}, status=401)
            if request.method == "POST" and request.content_type != "application/json":
                return web.json_response({"error": "expected JSON"}, status=415)
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    async def index(self, request: web.Request) -> web.Response:
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__API_TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html",
                            headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                                "connect-src 'self'; frame-ancestors 'none'"})

    # ------------------------------------------------------------------ read API
    async def api_state(self, request: web.Request) -> web.Response:
        c = self.core
        t = c.trader
        now = time.time()
        prices = []
        for s in list(dict.fromkeys([*c.cfg.symbols, *t.trading_symbols()])):
            tk = c.market.tickers.get(s) or {}
            last, op = tk.get("last"), tk.get("open24")
            prices.append({"symbol": s, "last": last, "age": None if c.market.age(s) == float("inf") else round(c.market.age(s), 1),
                           "change24": _f((last / op - 1) * 100, 2) if last and op else None,
                           "trading": s in t.trading_symbols()})
        session = None
        if t.state is not None:
            st = t.state
            d = t.engine.distances(st, c.market.prices(), now)
            snap = d["pnl"]
            positions = []
            for sym, pos in st.positions.items():
                price = c.market.price(sym) or pos.entry_price
                positions.append({"symbol": sym, "qty": pos.qty, "entry": pos.entry_price, "price": price,
                                  "cost": pos.cost_usdt, "pnl": _f(pos.unrealized(price, t.engine.fee_rate)),
                                  "stop": pos.stop_loss, "target": pos.take_profit, "opened_at": pos.opened_at,
                                  "oco": bool(pos.algo_id)})
            session = {"id": st.session_id, "status": st.status.value, "started_at": st.started_at, "ends_at": st.ends_at,
                       "params": st.params.to_dict(), "realized": _f(snap.realized), "unrealized": _f(snap.unrealized),
                       "total": _f(snap.total), "fees": _f(snap.fees), "open_cost": _f(snap.open_cost),
                       "max_loss": _f(d["max_loss"]), "to_max_loss": _f(d["to_max_loss"]), "target": _f(d["target"]),
                       "to_target": _f(d["to_target"]), "trades_opened": st.trades_opened, "trades_left": d["trades_left"],
                       "budget_free": _f(d["budget_free"]), "wins": st.wins, "losses": st.losses,
                       "consecutive_losses": st.consecutive_losses, "peak": _f(st.peak_pnl), "max_dd": _f(st.max_drawdown),
                       "profit_lock": st.profit_lock_active, "paused": bool(st.manual_paused or (st.paused_until and st.paused_until > now)),
                       "positions": positions}
        from .protections import compute_locks
        locks = [{"reason": lk.reason, "until": lk.until, "symbol": lk.symbol}
                 for lk in compute_locks(t.closed_trades(now), t.protections, now)] if t.protections.enabled else []
        day = t._day_start(now)
        cfg = c.cfg
        return web.json_response({
            "now": now, "uptime": now - self.started, "mode": t.mode, "live": c.ex.live,
            "live_code": t.live_code, "live_confirmed": t.live_confirmed,
            "trader_enabled": t.enabled, "paused": t.paused, "blockers": t.blockers(), "has_keys": c.ex.can_trade,
            "strategy": {k: getattr(t.strategy, k) for k in t.strategy.__dataclass_fields__} | {
                "signal_tf": t.strategy.signal_tf, "trend_tf": t.strategy.trend_tf},
            "strategy_mode": t.strategy_mode, "config_entry_mode": t.base_strategy.entry_mode,
            "session_presets": t.session_presets(),
            "exits": t.exits.__dict__, "session": session, "prices": prices,
            "health": {k: round(now - v, 1) for k, v in c.health.beats.items()}, "errors": c.health.errors,
            "pairs": {"enabled": c.pairlist.cfg.enabled, "symbols": c.pairlist.symbols(), "updated": c.pairlist.updated_at,
                      "metrics": c.pairlist.metrics[:30]},
            "locks": locks, "today_realized": _f(c.db.realized_pnl_between(day, now + 1)),
            "defaults": cfg.session_defaults.to_dict(), "hard_caps": cfg.hard_caps.__dict__, "daily_caps": cfg.daily_caps.__dict__,
            "schedule": cfg["schedule"], "alerts_muted_until": c.alerts.muted_until,
        })

    def _closed(self, since: float = 0.0) -> list[dict[str, Any]]:
        rows = self.core.db.trades("status='closed' AND exit_ts>=?", (since,))
        return [dict(r) for r in rows]

    async def api_trades(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 500)), 5000)
        rows = self.core.db.query("SELECT * FROM trades WHERE status IN ('open','closed','failed') "
                                  "ORDER BY id DESC LIMIT ?", (limit,))
        return web.json_response([dict(r) for r in rows])

    async def api_sessions(self, request: web.Request) -> web.Response:
        rows = self.core.db.query("SELECT id, started_at, ends_at, ended_at, status, stop_reason, mode, budget_usdt, "
                                  "realized_pnl, fees, trades, max_drawdown, report FROM sessions ORDER BY id DESC LIMIT 200")
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stop_reason_ru"] = STOP_REASON_RU[StopReason(d["stop_reason"])] if d["stop_reason"] else None
            except ValueError:
                d["stop_reason_ru"] = d["stop_reason"]
            out.append(d)
        return web.json_response(out)

    async def api_analytics(self, request: web.Request) -> web.Response:
        days = float(request.query.get("days", 0) or 0)
        since = time.time() - days * 86400 if days > 0 else 0.0
        return web.json_response(analytics(self._closed(since), self.core.cfg.tz))

    async def api_events(self, request: web.Request) -> web.Response:
        after = int(request.query.get("after", 0) or 0)
        kind = request.query.get("kind") or None
        return web.json_response(self.core.events.since(after, 300, kind))

    async def api_candles(self, request: web.Request) -> web.Response:
        sym = request.query.get("symbol", "BTC/USDT").upper()
        tf = request.query.get("tf", "5m")
        if tf not in ("1m", "5m", "1h"):
            tf = "5m"
        limit = min(int(request.query.get("limit", 288)), 1500)
        rows = self.core.db.candles(sym, tf, limit=limit, confirmed_only=False)
        t0 = rows[0][0] / 1000 if rows else 0
        trades = [dict(r) for r in self.core.db.trades("symbol=? AND entry_ts>=? AND status IN ('open','closed')", (sym, t0))]
        return web.json_response({"symbol": sym, "tf": tf, "candles": rows,
                                  "trades": [{"entry_ts": x["entry_ts"], "entry": x["entry_price"], "exit_ts": x["exit_ts"],
                                              "exit": x["exit_price"], "pnl": x["pnl_usdt"], "status": x["status"],
                                              "kind": x["exit_kind"]} for x in trades]})

    async def api_research(self, request: web.Request) -> web.Response:
        root = self.core.cfg.root / "data" / "research"
        out: dict[str, Any] = {}
        for name in ("results.json", "scalp_results.json"):
            f = root / name
            if f.exists():
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if name == "results.json" and request.query.get("full") != "1":
                    d.pop("curves", None)
                out[name.removesuffix(".json")] = d
        return web.json_response(out)

    # ------------------------------------------------------------------ control API
    async def _body(self, request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _msg(text: str, ok: bool = True) -> web.Response:
        return web.json_response({"ok": ok, "message": text})

    async def post_session_start(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        try:
            overrides = self.core.trader.parse_overrides({k: v for k, v in (body.get("params") or {}).items()
                                                          if v not in (None, "")})
        except ValueError as exc:
            return self._msg(f"❌ {exc}", False)
        msg = await self.core.trader.start_session(overrides, source="интерфейс")
        return self._msg(msg, msg.startswith("▶️"))

    async def post_session_end(self, request: web.Request) -> web.Response:
        return self._msg(await self.core.trader.end_session())

    async def post_session_set(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        msg = await self.core.trader.set_param(str(body.get("name", "")), str(body.get("value", "")))
        return self._msg(msg, msg.startswith("✅"))

    async def post_pause(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        return self._msg(self.core.trader.set_paused(bool(body.get("paused", True))))

    async def post_stop(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if body.get("confirm") != "STOP":
            return self._msg("Для аварийной остановки подтвердите действие.", False)
        return self._msg(await self.core.trader.emergency_stop("кнопка в интерфейсе"))

    async def post_strategy(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        msg = await self.core.trader.set_strategy_mode(str(body.get("mode", "")))
        return self._msg(msg, msg.startswith("✅"))

    async def post_trader(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        t = self.core.trader
        if body.get("enabled"):
            if t.stop_file.exists():
                return self._msg(f"Удалите файл {t.stop_file} перед включением.", False)
            t.set_enabled(True)
            b = t.blockers()
            return self._msg("✅ Trader включён." + (f" Но: {'; '.join(b)}" if b else ""))
        t.set_enabled(False)
        return self._msg("Trader выключен. Открытые позиции и активная сессия продолжают вестись.")

    async def post_confirm_live(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        msg = self.core.trader.confirm_live(str(body.get("code", "")))
        return self._msg(msg, msg.startswith("✅"))

    async def post_pairs_refresh(self, request: web.Request) -> web.Response:
        if not self.core.pairlist.cfg.enabled:
            return self._msg("Динамический отбор пар выключен (pairlist.enabled в config.yaml).", False)
        pairs = await self.core.pairlist.refresh()
        return self._msg("Пары: " + ", ".join(pairs))

    async def post_report(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if body.get("kind") == "weekly":
            await self.core.analytics.send_weekly()
        else:
            await self.core.analytics.send_digest()
        return self._msg("Отчёт добавлен в ленту событий.")

    async def post_mute(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        minutes = float(body.get("minutes", 60) or 0)
        self.core.alerts.muted_until = time.time() + minutes * 60
        return self._msg(f"Алерты скрыты на {minutes:.0f} мин." if minutes > 0 else "Алерты снова показываются.")


# ---------------------------------------------------------------------------- analytics (pure)
def analytics(trades: list[dict[str, Any]], tz: Any) -> dict[str, Any]:
    """Statistics over closed trades (dicts with the trades-table columns)."""
    tr = sorted((t for t in trades if t.get("exit_ts")), key=lambda t: t["exit_ts"])
    pnls = [float(t["pnl_usdt"] or 0) for t in tr]
    fees = [float(t["entry_fee_usdt"] or 0) + float(t["exit_fee_usdt"] or 0) for t in tr]
    eq = peak = dd = 0.0
    curve = []
    for t, p in zip(tr, pnls):
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        curve.append([t["exit_ts"], round(eq, 6)])
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p <= 0)
    n = len(pnls)
    holds = [(t["exit_ts"] - t["entry_ts"]) / 60 for t in tr if t.get("entry_ts")]

    daily: dict[str, dict[str, float]] = {}
    for t, p, f in zip(tr, pnls, fees):
        d = datetime.fromtimestamp(t["exit_ts"], tz).strftime("%Y-%m-%d")
        x = daily.setdefault(d, {"pnl": 0.0, "fees": 0.0, "trades": 0})
        x["pnl"] += p
        x["fees"] += f
        x["trades"] += 1
    pairs: dict[str, dict[str, float]] = {}
    for t, p, f in zip(tr, pnls, fees):
        x = pairs.setdefault(t["symbol"], {"pnl": 0.0, "fees": 0.0, "trades": 0, "wins": 0})
        x["pnl"] += p
        x["fees"] += f
        x["trades"] += 1
        x["wins"] += p > 0
    kinds: dict[str, int] = {}
    for t in tr:
        k = t.get("exit_kind") or "other"
        kinds[k] = kinds.get(k, 0) + 1
    hours = {h: {"pnl": 0.0, "trades": 0} for h in range(24)}
    for t, p in zip(tr, pnls):
        if t.get("entry_ts"):
            h = datetime.fromtimestamp(t["entry_ts"], tz).hour
            hours[h]["pnl"] += p
            hours[h]["trades"] += 1
    return {
        "summary": {"trades": n, "pnl": round(sum(pnls), 6), "fees": round(sum(fees), 6),
                    "gross": round(sum(pnls) + sum(fees), 6), "winrate": (sum(p > 0 for p in pnls) / n * 100) if n else 0.0,
                    "profit_factor": (gains / losses) if losses > 0 else None, "expectancy": (sum(pnls) / n) if n else 0.0,
                    "max_dd": round(dd, 6), "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
                    "avg_hold_min": (sum(holds) / len(holds)) if holds else None},
        "equity": curve,
        "daily": [{"date": d, **{k: round(v, 6) for k, v in x.items()}} for d, x in sorted(daily.items())],
        "pairs": sorted(({"symbol": s, **{k: round(v, 6) for k, v in x.items()},
                          "winrate": x["wins"] / x["trades"] * 100} for s, x in pairs.items()), key=lambda r: -r["pnl"]),
        "exits": kinds,
        "hours": [{"hour": h, **x} for h, x in hours.items()],
    }
