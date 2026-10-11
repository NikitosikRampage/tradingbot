"""Component heartbeats, 'bot is silent' alerts, heartbeat file for the external watchdog."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class Health:
    def __init__(self, heartbeat_file: Path, silent_after_sec: float, notify: Callable[[str], None]):
        self.beats: dict[str, float] = {}
        self.heartbeat_file = heartbeat_file
        self.silent_after = silent_after_sec
        self.notify = notify
        self.alerted: set[str] = set()
        self.started = time.time()
        self.errors: dict[str, str] = {}

    def beat(self, component: str) -> None:
        self.beats[component] = time.time()

    def error(self, component: str, msg: str) -> None:
        self.errors[component] = f"{time.strftime('%H:%M:%S')} {msg}"[:300]

    def silent(self) -> dict[str, float]:
        now = time.time()
        return {c: now - t for c, t in self.beats.items() if now - t > self.silent_after}

    def summary(self) -> str:
        now = time.time()
        lines = [f"{c}: {now - t:.0f}s назад" for c, t in sorted(self.beats.items())]
        return "\n".join(lines) or "нет данных"

    def write_file(self) -> None:
        try:
            self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heartbeat_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "beats": self.beats}), encoding="utf-8")
            tmp.replace(self.heartbeat_file)
        except OSError as exc:
            log.warning("heartbeat file: %s", exc)

    async def run(self, interval: float = 30.0) -> None:
        while True:
            self.beat("health")
            self.write_file()
            silent = self.silent()
            for comp, age in silent.items():
                if comp not in self.alerted:
                    self.alerted.add(comp)
                    self.notify(f"⚠️ Компонент «{comp}» молчит {age / 60:.1f} мин.")
            for comp in list(self.alerted):
                if comp not in silent:
                    self.alerted.discard(comp)
                    self.notify(f"✅ Компонент «{comp}» снова работает.")
            await asyncio.sleep(interval)
