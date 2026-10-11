"""Optional short market commentary from Claude, with a daily USD spend limit."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

SYSTEM = (
    "Ты — аналитик криптовалютного рынка. Тебе дают сводку цифр по спотовым парам OKX. "
    "Дай краткий разбор на русском (5–8 пунктов): общая картина, выделяющиеся монеты, "
    "ключевые уровни, на что обратить внимание. Без финансовых советов и без выдуманных данных: "
    "опирайся только на присланные цифры. Plain text, без Markdown-таблиц."
)


class LlmAnalyst:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.c = cfg["analytics"]["llm"]
        self.db = db
        self.client: Any = None
        if self.enabled:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=cfg.secrets.anthropic_key, timeout=120.0, max_retries=2)

    @property
    def enabled(self) -> bool:
        return bool(self.c.get("enabled")) and bool(self.cfg.secrets.anthropic_key)

    def _key(self) -> str:
        return "llm_spend:" + datetime.now(self.cfg.tz).strftime("%Y-%m-%d")

    def spent_today(self) -> float:
        return float(self.db.kv_get(self._key(), 0.0))

    def _cost(self, in_tok: int, out_tok: int) -> float:
        return (in_tok * float(self.c["price_input_per_mtok"])
                + out_tok * float(self.c["price_output_per_mtok"])) / 1_000_000

    async def summarize(self, numbers_text: str) -> str | None:
        if not self.enabled or self.client is None:
            return None
        budget = float(self.c["daily_budget_usd"])
        spent = self.spent_today()
        # worst case: whole max_tokens output + ~4 chars/token input
        worst = self._cost(len(numbers_text) // 3 + 500, int(self.c["max_tokens"]))
        if spent + worst > budget:
            log.info("LLM: дневной лимит %.2f$ (потрачено %.4f$) — пропуск", budget, spent)
            return None
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=self.c["model"],
            max_tokens=int(self.c["max_tokens"]),
            system=SYSTEM,
            messages=[{"role": "user", "content": numbers_text}],
            output_config={"effort": self.c.get("effort", "low")},
        )
        try:
            if self.c.get("refusal_fallback", True):
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            log.warning("LLM rate limit: %s", exc)
            return None
        except anthropic.APIStatusError as exc:
            log.warning("LLM API error %s: %s", exc.status_code, exc)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("LLM connection error: %s", exc)
            return None
        usage = resp.usage
        cost = self._cost(int(usage.input_tokens or 0), int(usage.output_tokens or 0))
        self.db.kv_set(self._key(), spent + cost)
        log.info("LLM: %d in / %d out токенов, %.4f$ (за день %.4f$)",
                 usage.input_tokens, usage.output_tokens, cost, spent + cost)
        if resp.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return text or None
