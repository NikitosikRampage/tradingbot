"""Pre-flight check before running the bot on OKX DEMO. Read-only: places no orders.

    python scripts/preflight.py

Checks: config + hard caps, Python version, API keys present (values are never printed),
DEMO mode, OKX public REST + WebSocket, authenticated demo account (USDT balance vs budget,
API key permissions), the local UI port.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
results: list[tuple[str, str, str]] = []


def report(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"{status} {name}" + (f": {detail}" if detail else ""), flush=True)


async def check_okx(cfg) -> None:
    import ccxt.async_support as ccxt
    import websockets

    from bot.exchange import net_options

    pub = ccxt.okx({"enableRateLimit": True, "timeout": 20000, **net_options()})
    try:
        t = await pub.fetch_ticker("BTC/USDT")
        report(OK, "OKX REST (публичные данные)", f"BTC/USDT = {t['last']}")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "OKX REST (публичные данные)", f"{type(exc).__name__}: {exc}")
    finally:
        await pub.close()

    try:
        async with websockets.connect(cfg["exchange"]["ws_public"], open_timeout=15) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]}))
            got = False
            for _ in range(5):
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if msg.get("data"):
                    got = True
                    break
            report(OK if got else FAIL, "OKX WebSocket (тикеры)", "данные идут" if got else "нет данных")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "OKX WebSocket (тикеры)", f"{type(exc).__name__}: {exc}")

    s = cfg.secrets
    if not (s.okx_key and s.okx_secret and s.okx_passphrase):
        prefix = "OKX_" if s.live_trading else "OKX_DEMO_"
        report(FAIL, "API-ключи OKX", f"в .env не заполнены {prefix}API_KEY / _SECRET / _PASSPHRASE")
        return
    report(OK, "API-ключи OKX", "заполнены (значения не показываются)")
    priv = ccxt.okx({"apiKey": s.okx_key, "secret": s.okx_secret, "password": s.okx_passphrase,
                     "enableRateLimit": True, "timeout": 20000, **net_options()})
    if not s.live_trading:
        priv.set_sandbox_mode(True)
    try:
        bal = await priv.fetch_balance()
        free = float((bal.get("free") or {}).get("USDT") or 0)
        need = cfg.session_defaults.budget_usdt
        report(OK if free >= need else FAIL, "Баланс демо-счёта" if not s.live_trading else "Баланс счёта",
               f"свободно {free:.2f} USDT, бюджет сессии {need:g} USDT"
               + ("" if free >= need else " — пополните демо-счёт в интерфейсе OKX (Demo trading)"))
        try:
            cfgres = await priv.private_get_account_config()
            perm = ((cfgres.get("data") or [{}])[0]).get("perm", "")
            if "withdraw" in perm.lower():
                report(FAIL, "Права ключа", f"{perm} — у ключа есть Withdraw, создайте ключ без вывода средств")
            else:
                report(OK, "Права ключа", perm or "read_only/trade")
        except Exception as exc:  # noqa: BLE001
            report(WARN, "Права ключа", f"не удалось проверить ({type(exc).__name__})")
    except ccxt.AuthenticationError as exc:
        hint = ("ключ создан не в режиме Demo trading?" if not s.live_trading else "проверьте ключ")
        report(FAIL, "Авторизация OKX", f"{exc} — {hint}")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "Авторизация OKX", f"{type(exc).__name__}: {exc}")
    finally:
        await priv.close()


def check_ui_port(cfg) -> None:
    import socket

    ui = cfg.raw.get("ui") or {}
    host, port = str(ui.get("host", "127.0.0.1")), int(ui.get("port", 8765))
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            report(OK, "Порт интерфейса", f"{host}:{port} свободен — интерфейс будет на http://{host}:{port}")
        except OSError:
            report(WARN, "Порт интерфейса", f"{host}:{port} занят (бот уже запущен?) — или смените ui.port в config.yaml")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    from bot.config import ConfigError, load_config

    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        report(WARN, "Python", f"{sys.version.split()[0]} (проверено на 3.11–3.13)")
    else:
        report(OK, "Python", sys.version.split()[0])
    if not Path(args.config).exists():
        report(FAIL, "config.yaml", "нет файла — скопируйте config.example.yaml в config.yaml")
        return 1
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        report(FAIL, "config.yaml", str(exc))
        return 1
    report(OK, "config.yaml", f"символы {', '.join(cfg.symbols)}; hard_caps: бюджет ≤ {cfg.hard_caps.max_budget_usdt:g}, "
                              f"убыток ≤ {cfg.hard_caps.max_loss_usdt:g}, длительность ≤ {cfg.hard_caps.max_duration_hours:g}ч")
    if not (ROOT / ".env").exists():
        report(FAIL, ".env", "нет файла — скопируйте .env.example в .env и заполните")
    if cfg.secrets.live_trading:
        report(FAIL, "Режим", "OKX_LIVE_TRADING=true — для демо-проверки поставьте false")
    else:
        report(OK, "Режим", "DEMO (x-simulated-trading)")
    report(OK if cfg["trader"]["enabled"] else WARN, "Trader",
           "включён" if cfg["trader"]["enabled"] else "выключен в config.yaml — включите здесь или кнопкой в интерфейсе")
    if (ROOT / "STOP").exists():
        report(FAIL, "Файл STOP", "существует — удалите, иначе trader не включится")
    asyncio.run(check_okx(cfg))
    check_ui_port(cfg)
    fails = [r for r in results if r[0] == FAIL]
    print("\n" + ("Готово к запуску на DEMO: start.bat" if not fails else f"Исправьте {len(fails)} пункт(а) выше и запустите проверку снова."))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.platform == "win32" and sys.version_info < (3, 14):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    sys.exit(main())
