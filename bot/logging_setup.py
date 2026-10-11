"""Rotating file logs + redaction of secrets."""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path


class SecretFilter(logging.Filter):
    """Replaces any known secret value (API keys, tokens) with ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]
        # telegram bot token pattern in URLs logged by httpx
        self.token_re = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")

    def _clean(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return self.token_re.sub("bot***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = self._clean(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        return True


def setup_logging(log_dir: Path, level: str = "INFO", secrets: list[str] | None = None,
                  max_bytes: int = 10_000_000, backup_count: int = 10) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    flt = SecretFilter(secrets or [])
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    main = logging.handlers.RotatingFileHandler(log_dir / "bot.log", maxBytes=max_bytes,
                                                backupCount=backup_count, encoding="utf-8")
    err = logging.handlers.RotatingFileHandler(log_dir / "errors.log", maxBytes=max_bytes,
                                               backupCount=backup_count, encoding="utf-8")
    err.setLevel(logging.WARNING)
    handlers: list[logging.Handler] = [main, err]
    if sys.stdout is not None and sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))  # interactive run only
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(flt)
        root.addHandler(h)
    # noisy libraries
    for name in ("httpx", "httpx2", "telegram.ext", "websockets", "ccxt", "asyncio", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)
