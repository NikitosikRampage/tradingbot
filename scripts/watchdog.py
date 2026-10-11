"""External watchdog (run every 5 min by Task Scheduler): Telegram alert if the bot is silent.

Works even when the bot process is dead, because it is a separate process.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def send(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)


def main() -> int:
    from bot.config import load_config

    cfg = load_config(with_secrets=True)
    token, chat = cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id
    limit = float(cfg["health"]["silent_alert_minutes"]) * 60
    hb = cfg.path("heartbeat_file")
    state_file = ROOT / "data" / "watchdog_state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"alerted": False}
    age = time.time() - json.loads(hb.read_text())["ts"] if hb.exists() else float("inf")
    silent = age > limit
    msg = None
    if silent and not state.get("alerted"):
        msg = (f"🚨 Бот молчит {age / 60:.0f} мин (heartbeat не обновляется)." if age != float("inf")
               else "🚨 Бот не запущен (нет heartbeat).")
        state["alerted"] = True
    elif not silent and state.get("alerted"):
        msg = "✅ Бот снова отвечает."
        state["alerted"] = False
    if msg and token and chat:
        try:
            send(token, str(chat), msg)
        except Exception as exc:  # noqa: BLE001
            print(f"telegram: {exc}", file=sys.stderr)
            return 1
    state_file.parent.mkdir(exist_ok=True)
    state_file.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
