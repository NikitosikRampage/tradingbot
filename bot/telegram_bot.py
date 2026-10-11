"""Telegram command interface. Answers ONLY to TELEGRAM_CHAT_ID."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

HELP = """Команды:
/status — состояние бота
/session_start budget=10 hours=8 max_loss=2 target=1.5 — запустить сессию
   (ещё: max_loss_pct, target_pct, lock=on, trail=30, trades=10, losses=3, end=close_all|manage_only, sl=2, tp=4, cutoff=4)
/session_status — время, бюджет, PnL, расстояние до лимитов
/session_set <параметр> <значение> — изменить лимит на лету (в пределах hard_caps)
/session_end — завершить сессию (по on_session_end)
/pnl, /positions
/pause, /resume — пауза/возобновление новых входов
/stop — АВАРИЙНО: закрыть всё и выключить trader
/trader_on, /trader_off — включить/выключить trader
/confirm_live <код> — подтверждение реального счёта
/pairs — текущие торгуемые пары и почему, /protections — активные блокировки входов
/digest, /weekly — отчёты сейчас
/mute <мин> — заглушить алерты, /health — heartbeat компонентов"""


def parse_kv(args: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in args:
        if "=" not in a:
            raise ValueError(f"Ожидается key=value, получено: {a}")
        k, v = a.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class TelegramBot:
    def __init__(self, app: "App", token: str, chat_id: int):
        self.core = app
        self.chat_id = chat_id
        self.application = Application.builder().token(token).build()
        only_me = filters.Chat(chat_id=chat_id)
        cmds: dict[str, Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]] = {
            "start": self.cmd_help, "help": self.cmd_help, "status": self.cmd_status,
            "session_start": self.cmd_session_start, "session_status": self.cmd_session_status,
            "session_set": self.cmd_session_set, "session_end": self.cmd_session_end,
            "pnl": self.cmd_pnl, "positions": self.cmd_positions, "pause": self.cmd_pause,
            "resume": self.cmd_resume, "stop": self.cmd_stop, "trader_on": self.cmd_trader_on,
            "trader_off": self.cmd_trader_off, "confirm_live": self.cmd_confirm_live,
            "digest": self.cmd_digest, "weekly": self.cmd_weekly, "mute": self.cmd_mute,
            "health": self.cmd_health, "pairs": self.cmd_pairs, "protections": self.cmd_protections,
        }
        for name, fn in cmds.items():
            self.application.add_handler(CommandHandler(name, self._guard(fn), filters=only_me))
        # anything from other chats -> logged and ignored
        self.application.add_handler(MessageHandler(~only_me, self._intruder))
        self.application.add_error_handler(self._on_error)

    def _guard(self, fn: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                await fn(update, ctx)
            except Exception as exc:  # noqa: BLE001
                log.exception("command failed")
                await self._reply(update, f"Ошибка: {exc}")
        return wrapper

    async def _intruder(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        log.warning("Сообщение из чужого чата %s проигнорировано", chat.id if chat else "?")

    async def _on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.warning("telegram error: %s", ctx.error)

    async def start(self) -> None:
        await self.application.initialize()
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(drop_pending_updates=True)
        self.core.notifier.attach(self.application.bot)

    async def stop(self) -> None:
        try:
            if self.application.updater is not None:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("telegram stop")

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        if update.effective_message is not None:
            for i in range(0, len(text), 4000):
                await update.effective_message.reply_text(text[i:i + 4000])

    # ------------------------------------------------------------------ commands
    async def cmd_help(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, HELP)

    async def cmd_status(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        t = self.core.trader
        up = time.time() - self.core.health.started
        await self._reply(u, f"{t.status_text()}\n{t.next_schedule_hint()}\nАптайм: {up / 3600:.1f} ч")

    async def cmd_session_start(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            overrides = self.core.trader.parse_overrides(parse_kv(c.args or []))
        except ValueError as exc:
            await self._reply(u, f"❌ {exc}")
            return
        msg = await self.core.trader.start_session(overrides, source="команда")
        if not msg.startswith("▶️") and not msg.startswith("❌"):
            await self._reply(u, msg)

    async def cmd_session_status(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        t = self.core.trader
        text = t.session_status_text()
        if t.state is not None and t.state.positions:
            text += "\n\n" + t.positions_text()
        await self._reply(u, text)

    async def cmd_session_set(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        args = c.args or []
        if len(args) == 1 and "=" in args[0]:
            args = args[0].split("=", 1)
        if len(args) != 2:
            await self._reply(u, "Формат: /session_set <параметр> <значение>, напр. /session_set max_loss 1.5")
            return
        await self._reply(u, await self.core.trader.set_param(args[0], args[1]))

    async def cmd_session_end(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, await self.core.trader.end_session())

    async def cmd_pnl(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, self.core.trader.pnl_text())

    async def cmd_positions(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, self.core.trader.positions_text())

    async def cmd_pause(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, self.core.trader.set_paused(True))

    async def cmd_resume(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, self.core.trader.set_paused(False))

    async def cmd_stop(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self.core.trader.emergency_stop("команда /stop")

    async def cmd_trader_on(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        t = self.core.trader
        if t.stop_file.exists():
            await self._reply(u, f"Удалите файл {t.stop_file} перед включением.")
            return
        t.set_enabled(True)
        b = t.blockers()
        await self._reply(u, "✅ Trader включён." + (f" Но: {'; '.join(b)}" if b else
                                                    " Запустите сессию: /session_start"))

    async def cmd_trader_off(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        self.core.trader.set_enabled(False)
        await self._reply(u, "Trader выключен (открытые позиции и активная сессия продолжают вестись; "
                             "для закрытия — /session_end или /stop).")

    async def cmd_confirm_live(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, self.core.trader.confirm_live(" ".join(c.args or [])))

    async def cmd_digest(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self.core.analytics.send_digest()

    async def cmd_weekly(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self.core.analytics.send_weekly()

    async def cmd_mute(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            minutes = float((c.args or ["60"])[0])
        except ValueError:
            minutes = 60.0
        self.core.alerts.muted_until = time.time() + minutes * 60
        await self._reply(u, f"🔕 Алерты заглушены на {minutes:.0f} мин." if minutes > 0 else "🔔 Алерты включены.")

    async def cmd_pairs(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        if c.args and c.args[0].lower() == "refresh" and self.core.pairlist.cfg.enabled:
            await self._reply(u, "Пересчитываю список пар...")
            await self.core.pairlist.refresh()
        await self._reply(u, self.core.pairlist.text())

    async def cmd_protections(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(u, self.core.trader.protections_text())

    async def cmd_health(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        h = self.core.health
        errs = "\n".join(f"{k}: {v}" for k, v in h.errors.items()) or "нет"
        await self._reply(u, f"Heartbeat:\n{h.summary()}\n\nПоследние ошибки:\n{errs}")
