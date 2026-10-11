"""Supervisor: restarts main.py after a crash. Stops when the SHUTDOWN file appears (stop.bat).

Windows: run it with pythonw.exe (no console window) — start.bat and Task Scheduler do that.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHUTDOWN = ROOT / "SHUTDOWN"
LOGS = ROOT / "logs"

EXIT_CONFIG_ERROR = 2
EXIT_ALREADY_RUNNING = 3


def main() -> int:
    LOGS.mkdir(exist_ok=True)
    SHUTDOWN.unlink(missing_ok=True)
    fails = 0
    while True:
        started = time.time()
        with (LOGS / "console.log").open("a", encoding="utf-8") as out:
            out.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} start main.py ===\n")
            out.flush()
            rc = subprocess.call([sys.executable, str(ROOT / "main.py")], cwd=ROOT, stdout=out,
                                 stderr=subprocess.STDOUT)
            out.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} exit code {rc} ===\n")
        if SHUTDOWN.exists():
            SHUTDOWN.unlink(missing_ok=True)
            return 0
        if rc == EXIT_ALREADY_RUNNING:
            return rc
        fails = fails + 1 if time.time() - started < 120 else 0
        delay = 60 if rc == EXIT_CONFIG_ERROR else min(10 * (fails or 1), 300)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
