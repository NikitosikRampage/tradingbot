"""Event log: everything the bot reports (trades, sessions, alerts, health, digests).

Replaces the Telegram notifier: events are stored in SQLite and shown in the local web UI.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from .db import Database

log = logging.getLogger(__name__)

# rough category from the message, used for filtering and colouring in the UI
_KINDS = (("🟢 ВХОД", "trade"), ("✅ ВЫХОД", "trade"), ("🔴 ВЫХОД", "trade"), ("🏁", "session"),
          ("▶️ Сессия", "session"), ("❌", "error"), ("⚠️", "warning"), ("🛑", "error"), ("🚨", "error"),
          ("☀️", "report"), ("📅", "report"), ("🤖", "report"))


def classify(text: str) -> str:
    t = text.split("] ", 1)[-1] if text.startswith("[") else text
    for prefix, kind in _KINDS:
        if t.startswith(prefix):
            return kind
    if any(t.startswith(e) for e in ("📈", "📉", "🚀 ", "🔻", "↩️", "↪️", "🔥", "🧊", "📊", "✨", "☠️")):
        return "alert"
    return "info"


class EventLog:
    def __init__(self, db: Database, keep: int = 500):
        self.db = db
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep)

    def send(self, text: str, kind: str | None = None) -> None:
        """Same signature as the old notifier: ``notify(text)``."""
        kind = kind or classify(text)
        ts = time.time()
        log.info("EVENT[%s]: %s", kind, text.replace("\n", " | ")[:500])
        try:
            cur = self.db.execute("INSERT INTO events(ts, kind, text) VALUES(?,?,?)", (ts, kind, text))
            eid = int(cur.lastrowid)
        except Exception:  # noqa: BLE001
            log.exception("event insert")
            eid = 0
        self.recent.append({"id": eid, "ts": ts, "kind": kind, "text": text})

    def since(self, after_id: int = 0, limit: int = 200, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, ts, kind, text FROM events WHERE id > ?"
        args: list[Any] = [after_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) for r in reversed(rows)]
