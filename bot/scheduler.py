"""Time-based jobs in the configured timezone: digest, weekly report, scheduled sessions."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from .analytics import Analytics
from .config import Config
from .db import Database
from .trader import Trader

log = logging.getLogger(__name__)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def slot_today(now: datetime, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


class Scheduler:
    CHECK_SEC = 20.0

    def __init__(self, cfg: Config, db: Database, analytics: Analytics, trader: Trader):
        self.cfg = cfg
        self.db = db
        self.analytics = analytics
        self.trader = trader
        self._last_check = time.time()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.CHECK_SEC)
            now_ts = time.time()
            prev = self._last_check
            self._last_check = now_ts
            try:
                await self._tick(prev, now_ts)
            except Exception:  # noqa: BLE001
                log.exception("scheduler")

    async def _tick(self, prev_ts: float, now_ts: float) -> None:
        a = self.cfg["analytics"]
        now = datetime.fromtimestamp(now_ts, self.cfg.tz)
        today = now.strftime("%Y-%m-%d")
        # digest: catch up within 3h if the PC was off/asleep at 09:00
        if a.get("digest_enabled"):
            slot = slot_today(now, a["digest_time"])
            if slot <= now <= slot + timedelta(hours=3) and self.db.kv_get("last_digest") != today:
                self.db.kv_set("last_digest", today)
                await self.analytics.send_digest()
        if a.get("weekly_enabled") and DAYS[now.weekday()] == str(a["weekly_day"]).lower()[:3]:
            slot = slot_today(now, a["weekly_time"])
            if slot <= now <= slot + timedelta(hours=6) and self.db.kv_get("last_weekly") != today:
                self.db.kv_set("last_weekly", today)
                await self.analytics.send_weekly()
        await self._session_schedule(prev_ts, now_ts)

    async def _session_schedule(self, prev_ts: float, now_ts: float) -> None:
        sc: dict[str, Any] = self.cfg["schedule"]
        if not sc.get("enabled"):
            return
        # Fire only if the slot was crossed while the bot was running normally (consecutive checks).
        # After a restart or a wake-up from sleep the slot is NOT caught up: no automatic new session.
        if now_ts - prev_ts > self.CHECK_SEC * 6:
            return
        now = datetime.fromtimestamp(now_ts, self.cfg.tz)
        slot = slot_today(now, sc["start_time"])
        if DAYS[slot.weekday()] not in [d.lower()[:3] for d in sc["days"]]:
            return
        if not (prev_ts < slot.timestamp() <= now_ts):
            return
        key = slot.strftime("%Y-%m-%d %H:%M")
        if self.db.kv_get("last_scheduled_session") == key:
            return
        self.db.kv_set("last_scheduled_session", key)
        overrides = dict(sc.get("params") or {})
        msg = await self.trader.start_session(overrides, source="расписание")
        log.info("scheduled session: %s", msg)
        if not msg.startswith("▶️") and not msg.startswith("❌"):
            self.trader.notify(f"Сессия по расписанию не запущена: {msg}")
