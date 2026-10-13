"""External watchdog (run every 5 min by Task Scheduler): Windows notification if the bot is silent.

Works even when the bot process is dead, because it is a separate process. Also writes the
incident to logs/watchdog.log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def notify(title: str, text: str) -> None:
    """Balloon notification in the Windows tray (no extra modules); elsewhere just print."""
    log = ROOT / "logs" / "watchdog.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {title}: {text}\n")
    if os.name != "nt":
        print(f"{title}: {text}")
        return
    ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Warning; "
          "$n.Visible = $true; $n.ShowBalloonTip(15000, $env:WD_TITLE, $env:WD_TEXT, 'Warning'); Start-Sleep -Seconds 16; $n.Dispose()")
    env = dict(os.environ, WD_TITLE=title, WD_TEXT=text)
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], env=env,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> int:
    from bot.config import load_config

    cfg = load_config(with_secrets=False)
    limit = float(cfg["health"]["silent_alert_minutes"]) * 60
    hb = cfg.path("heartbeat_file")
    state_file = ROOT / "data" / "watchdog_state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"alerted": False}
    age = time.time() - json.loads(hb.read_text())["ts"] if hb.exists() else float("inf")
    silent = age > limit
    if silent and not state.get("alerted"):
        notify("OKX Bot молчит", f"Нет признаков жизни {age / 60:.0f} мин." if age != float("inf")
               else "Бот не запущен (нет heartbeat).")
        state["alerted"] = True
    elif not silent and state.get("alerted"):
        notify("OKX Bot снова работает", "Heartbeat обновляется.")
        state["alerted"] = False
    state_file.parent.mkdir(exist_ok=True)
    state_file.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
