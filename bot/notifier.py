"""Outgoing Telegram messages through a queue (rate-limited, retried, never blocks trading)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

MAX_LEN = 4000


class Notifier:
    def __init__(self, chat_id: int | None, min_interval: float = 1.1):
        self.chat_id = chat_id
        self.bot: Any = None          # telegram.Bot, attached when the Telegram app starts
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self.min_interval = min_interval

    def attach(self, bot: Any) -> None:
        self.bot = bot

    def send(self, text: str) -> None:
        """Fire-and-forget. Long messages are split."""
        log.info("NOTIFY: %s", text.replace("\n", " | ")[:500])
        for i in range(0, len(text), MAX_LEN):
            try:
                self.queue.put_nowait(text[i:i + MAX_LEN])
            except asyncio.QueueFull:
                log.warning("Очередь уведомлений переполнена, сообщение отброшено")

    async def run(self) -> None:
        from telegram.error import NetworkError, RetryAfter, TimedOut

        while True:
            text = await self.queue.get()
            if self.bot is None or self.chat_id is None:
                continue
            for attempt in range(5):
                try:
                    await self.bot.send_message(chat_id=self.chat_id, text=text,
                                                disable_web_page_preview=True)
                    break
                except RetryAfter as exc:
                    await asyncio.sleep(float(getattr(exc, "retry_after", 5)) + 0.5)
                except (TimedOut, NetworkError) as exc:
                    log.warning("Telegram send: %s", exc)
                    await asyncio.sleep(2 ** attempt)
                except Exception:  # noqa: BLE001
                    log.exception("Telegram send failed")
                    break
            await asyncio.sleep(self.min_interval)
