"""Local web UI (auth, host check, controls) and the event log."""
import asyncio
import re
import time

from aiohttp.test_utils import TestClient, TestServer

from bot.app import App
from bot.config import load_config
from bot.db import Database
from bot.events import EventLog, classify
from bot.web import analytics

from test_trader import FakeExchange


def make_app(tmp_path, **ui):
    cfg = load_config(tmp_path / "none.yaml", root=tmp_path, with_secrets=False)
    cfg.raw["trader"]["enabled"] = True
    cfg.raw["ui"].update(ui)
    app = App(cfg)
    ex = FakeExchange()
    app.ex = app.collector.ex = app.trader.ex = ex
    for s in ("BTC/USDT", "ETH/USDT"):
        ex.prices[s] = 100.0
        app.market.update(s, {"last": 100.0})
    return app


async def client_for(app):
    server = TestServer(app.web.build_app())
    client = TestClient(server)
    await client.start_server()
    app.web.allowed_hosts = {f"127.0.0.1:{server.port}"}
    return client


def call(tmp_path, fn, **ui):
    async def go():
        app = make_app(tmp_path, **ui)
        client = await client_for(app)
        try:
            return await fn(app, client, {"X-Token": app.web.token})
        finally:
            await client.close()
            app.db.close()
    return asyncio.run(go())


def test_index_embeds_token_and_api_requires_it(tmp_path):
    async def fn(app, c, h):
        r = await c.get("/")
        html = await r.text()
        assert r.status == 200 and app.web.token in html and "__API_TOKEN__" not in html
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert (await c.get("/api/state")).status == 401
        assert (await c.get("/api/state", headers={"X-Token": "wrong"})).status == 401
        r = await c.get("/api/state", headers=h)
        st = await r.json()
        assert r.status == 200 and st["mode"] == "DEMO" and st["session"] is None
    call(tmp_path, fn)


def test_foreign_host_header_rejected(tmp_path):
    """DNS-rebinding guard: a page on another origin cannot talk to the UI."""
    async def fn(app, c, h):
        assert (await c.get("/", headers={"Host": "evil.example:8765"})).status == 403
        assert (await c.get("/api/state", headers={**h, "Host": "evil.example"})).status == 403
    call(tmp_path, fn)


def test_post_requires_json(tmp_path):
    async def fn(app, c, h):
        r = await c.post("/api/pause", data="paused=1", headers={**h, "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status == 415
    call(tmp_path, fn)


def test_non_loopback_host_is_ignored(tmp_path):
    async def fn(app, c, h):
        assert app.web.host == "127.0.0.1" and app.web.url.startswith("http://127.0.0.1:")
    call(tmp_path, fn, host="0.0.0.0")


def test_session_start_respects_hard_caps_and_emergency_stop_needs_confirm(tmp_path):
    async def fn(app, c, h):
        cap = app.cfg.hard_caps.max_budget_usdt
        r = await (await c.post("/api/session/start", json={"params": {"budget_usdt": cap * 10}}, headers=h)).json()
        assert not r["ok"] and app.trader.state is None
        r = await (await c.post("/api/session/start", json={"params": {"budget_usdt": 10}}, headers=h)).json()
        assert r["ok"], r["message"]
        assert app.trader.state is not None
        # the UI cannot raise the loss limit above the hard cap either
        r = await (await c.post("/api/session/set", json={"name": "max_loss_usdt",
                                                          "value": str(app.cfg.hard_caps.max_loss_usdt * 10)}, headers=h)).json()
        assert not r["ok"]
        r = await (await c.post("/api/stop", json={}, headers=h)).json()
        assert not r["ok"] and app.trader.enabled
        r = await (await c.post("/api/stop", json={"confirm": "STOP"}, headers=h)).json()
        assert not app.trader.enabled and app.trader.state is None
        ev = await (await c.get("/api/events", headers=h)).json()
        assert any(e["kind"] == "session" for e in ev)
    call(tmp_path, fn)


def test_trader_cannot_be_enabled_while_stop_file_exists(tmp_path):
    async def fn(app, c, h):
        app.trader.set_enabled(False)
        app.trader.stop_file.write_text("")
        r = await (await c.post("/api/trader", json={"enabled": True}, headers=h)).json()
        assert not r["ok"] and not app.trader.enabled
    call(tmp_path, fn)


def test_live_confirmation_code_is_not_exposed_in_demo(tmp_path):
    async def fn(app, c, h):
        st = await (await c.get("/api/state", headers=h)).json()
        assert not st["live"]
        r = await (await c.post("/api/confirm_live", json={"code": "000000"}, headers=h)).json()
        assert not r["ok"]
    call(tmp_path, fn)


def test_analytics_pure():
    from zoneinfo import ZoneInfo
    t0 = 1_700_000_000
    rows = [
        {"symbol": "BTC/USDT", "entry_ts": t0, "exit_ts": t0 + 600, "pnl_usdt": 0.3, "entry_fee_usdt": 0.01,
         "exit_fee_usdt": 0.01, "exit_kind": "tp"},
        {"symbol": "ETH/USDT", "entry_ts": t0 + 700, "exit_ts": t0 + 1300, "pnl_usdt": -0.5, "entry_fee_usdt": 0.01,
         "exit_fee_usdt": 0.01, "exit_kind": "sl"},
        {"symbol": "BTC/USDT", "entry_ts": t0 + 1400, "exit_ts": t0 + 2000, "pnl_usdt": 0.1, "entry_fee_usdt": 0.01,
         "exit_fee_usdt": 0.01, "exit_kind": "tp"},
    ]
    a = analytics(rows, ZoneInfo("UTC"))
    s = a["summary"]
    assert s["trades"] == 3 and abs(s["pnl"] + 0.1) < 1e-9 and abs(s["fees"] - 0.06) < 1e-9
    assert abs(s["max_dd"] - 0.5) < 1e-9 and abs(s["profit_factor"] - 0.8) < 1e-9
    assert [p[1] for p in a["equity"]] == [0.3, -0.2, -0.1]
    assert a["exits"] == {"tp": 2, "sl": 1}
    assert a["pairs"][0]["symbol"] == "BTC/USDT" and a["pairs"][0]["trades"] == 2
    assert sum(h["trades"] for h in a["hours"]) == 3 and abs(s["avg_hold_min"] - 10) < 1e-9


def test_event_log_and_classify(tmp_path):
    db = Database(tmp_path / "e.db")
    ev = EventLog(db)
    ev.send("🟢 ВХОД BTC/USDT")
    ev.send("▶️ Сессия #1 запущена")
    ev.send("⚠️ что-то не так")
    ev.send("просто текст")
    all_ = ev.since(0, 100, None)
    assert [e["kind"] for e in all_] == ["trade", "session", "warning", "info"]
    last = all_[-1]["id"]
    ev.send("❌ ошибка")
    new = ev.since(last, 100, None)
    assert len(new) == 1 and new[0]["kind"] == "error"
    assert [e["kind"] for e in ev.since(0, 100, "trade")] == ["trade"]
    assert classify("🚨 АВАРИЙНАЯ ОСТАНОВКА") in ("error", "session", "warning")
    db.close()


def test_url_file_written_for_start_bat(tmp_path):
    async def go():
        app = make_app(tmp_path, port=0)
        app.web.port = _free_port()
        await app.web.start()
        try:
            f = app.web.url_file
            assert f.exists() and re.fullmatch(r"http://127\.0\.0\.1:\d+", f.read_text())
        finally:
            await app.web.stop()
            app.db.close()
        assert not app.web.url_file.exists()
    asyncio.run(go())


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_events_are_trimmed_by_time_order(tmp_path):
    db = Database(tmp_path / "e.db")
    ev = EventLog(db)
    for i in range(5):
        ev.send(f"msg {i}")
    out = ev.since(0, 3, None)
    assert len(out) == 3 and all(out[i]["id"] < out[i + 1]["id"] for i in range(2))
    assert out[-1]["ts"] <= time.time()
    db.close()
