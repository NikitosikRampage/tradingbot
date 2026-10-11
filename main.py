"""Entry point: python main.py [--config config.yaml]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from bot.app import App
from bot.config import ROOT, ConfigError, load_config
from bot.logging_setup import setup_logging

log = logging.getLogger("main")


class SingleInstance:
    """Cross-platform exclusive lock file, so Task Scheduler + manual start can't run two bots."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True


async def amain(app: App) -> None:
    loop = asyncio.get_running_loop()

    def _stop(*_: object) -> None:
        app.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_stop))  # Windows
    run_task = asyncio.create_task(app.run(), name="app")
    stop_task = asyncio.create_task(app.stop_event.wait(), name="stop")
    try:
        done, _ = await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if run_task in done:
            run_task.result()  # re-raise a fatal error -> non-zero exit -> supervisor restarts
    finally:
        for t in (run_task, stop_task):
            t.cancel()
        await asyncio.gather(run_task, stop_task, return_exceptions=True)
        await app.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="OKX monitoring & light trading bot")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    lg = cfg["logging"]
    setup_logging(cfg.path("logs"), lg["level"], cfg.secrets.values(), int(lg["max_bytes"]),
                  int(lg["backup_count"]))
    lock = SingleInstance(cfg.root / "data" / "bot.lock")
    if not lock.acquire():
        log.error("Бот уже запущен (data/bot.lock занят)")
        return 3
    if sys.platform == "win32" and sys.version_info < (3, 14):
        # aiohttp (ccxt) is most reliable on the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    app = App(cfg)
    try:
        asyncio.run(amain(app))
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        log.exception("Фатальная ошибка")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
