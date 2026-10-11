# OKX Monitor & Light Trader

Бот для круглосуточного мониторинга рынка и лёгкой спотовой торговли на OKX.
Python 3.11, ccxt + websockets, SQLite, Telegram (python-telegram-bot). Работает локально на
Windows 11 (venv, без Docker), код кроссплатформенный (пути через `pathlib`).

> ⚠️ Торговля по умолчанию **выключена** и работает в **DEMO**-режиме OKX
> (заголовок `x-simulated-trading: 1`). Только спот, только лонги, без плеча и фьючерсов.
> Никакая стратегия не гарантирует прибыль: сначала прогоните бэктест и поработайте на демо.

## Содержание
1. [Возможности](#возможности)
2. [Структура проекта](#структура-проекта)
3. [Установка](#установка)
4. [Ключи OKX](#ключи-okx)
5. [Telegram-бот](#telegram-бот)
6. [Конфигурация (config.yaml)](#конфигурация)
7. [Сессии и лимиты](#сессии-и-лимиты)
8. [Команды Telegram](#команды-telegram)
9. [Надёжность](#надёжность)
10. [Запуск 24/7 на Windows](#запуск-247-на-windows) (подробно — [docs/WINDOWS.md](docs/WINDOWS.md))
11. [Бэктест](#бэктест)
12. [Тесты](#тесты)
13. [Чеклист перехода с демо на реал](#чеклист-перехода-с-демо-на-реал)

## Возможности

| Модуль | Что делает |
|---|---|
| **collector** | WebSocket OKX: свечи 1m/5m/1h (`/ws/v5/business`) и тикеры (`/ws/v5/public`) по списку монет. Автореконнект с экспоненциальной паузой, ping/pong, backfill пропусков через REST после каждого (пере)подключения, хранение в SQLite, очистка старых 1m/5m. |
| **alerts** | Движение > X% за N минут · пробой и отскок от 24h/7d high/low · RSI(14) 1h > 75 / < 25 · всплеск объёма > 3x среднего · пересечение EMA 50/200 на 4h. Cooldown на каждое правило и монету (переживает рестарт), `/mute`. |
| **analytics** | Утренний дайджест 09:00 (Europe/Moscow): изменение 24h, волатильность, уровни (24h/7d, pivot/R1/S1), RSI, тренд 4h, корреляция с BTC. Недельный отчёт в воскресенье. Опционально — краткий разбор от Claude (модель из конфига, ключ в `.env`, дневной лимит $). |
| **trader** | Выключен по умолчанию. Тренд EMA50 > EMA200 на 4h + вход по откату RSI(14) на 1h, только лонги. ≤ 1 позиции на монету, ≤ 2 одновременно, размер ≤ 30% бюджета сессии, SL 2% / TP 4% (настраиваются). SL/TP контролирует бот на каждом тике **и** OCO-ордер на бирже (защищает, пока ПК спит). Каждая сделка и причина входа — в БД и в Telegram. Комиссии учитываются в PnL. |
| **risk/session** | Торговые «сессии» с бюджетом, таймером, лимитом убытка, целью/profit-lock, лимитами сделок/серии убытков, дневными лимитами и неизменяемыми из Telegram `hard_caps`. Чистая логика без I/O — та же используется в бэктесте и покрыта тестами. |

## Структура проекта

```
tradingbot/
├── main.py                 # точка входа (single-instance lock, graceful shutdown)
├── run_forever.py          # супервизор: перезапуск main.py при падении, стоп по файлу SHUTDOWN
├── backtest.py             # бэктест стратегии с теми же лимитами сессии
├── config.example.yaml     # пример конфига -> скопировать в config.yaml
├── .env.example            # пример секретов  -> скопировать в .env
├── setup.bat / start.bat / stop.bat
├── requirements.txt, requirements-dev.txt
├── bot/
│   ├── config.py           # загрузка config.yaml + .env, валидация против hard_caps
│   ├── risk.py             # ★ движок сессий и лимитов (pure)
│   ├── strategy.py         # сигнал EMA50/200 4h + RSI 1h (pure)
│   ├── indicators.py       # EMA, RSI, корреляция, ресемплинг 1h->4h
│   ├── trader.py           # исполнение: сессии, вход/выход, OCO, сверка с биржей, восстановление
│   ├── exchange.py         # ccxt OKX: ретраи, rate limits, ордера по clOrdId, OCO
│   ├── collector.py        # WebSocket + backfill
│   ├── alerts.py           # правила алертов + cooldown
│   ├── analytics.py, llm.py# дайджест/недельный отчёт, Claude
│   ├── scheduler.py        # дайджест, отчёт, сессии по расписанию
│   ├── telegram_bot.py     # команды (только ваш chat_id)
│   ├── notifier.py         # очередь исходящих сообщений
│   ├── health.py           # heartbeat компонентов, алерт «молчит > 5 мин»
│   ├── db.py               # SQLite (WAL)
│   └── logging_setup.py    # ротация логов, маскирование секретов
├── scripts/
│   ├── install_task.ps1    # Task Scheduler: автозапуск при входе + watchdog каждые 5 мин
│   ├── uninstall_task.ps1
│   ├── install_nssm.bat    # вариант: Windows-служба через NSSM
│   └── watchdog.py         # внешний сторож: Telegram-алерт, если бот молчит
├── tests/                  # pytest: risk/session, trader (fake exchange), индикаторы, алерты, бэктест
├── docs/WINDOWS.md
├── data/                   # bot.db, heartbeat.json, кэш бэктеста (создаётся автоматически)
└── logs/                   # bot.log, errors.log (ротация), console.log
```

## Установка

Нужен Python **3.11** (64-bit) с python.org, при установке отметьте «Add python.exe to PATH».

```bat
git clone <repo> C:\bots\tradingbot
cd C:\bots\tradingbot
setup.bat
```

`setup.bat` создаёт `.venv`, ставит зависимости и копирует `config.example.yaml → config.yaml`,
`.env.example → .env`. Вручную то же самое:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy config.example.yaml config.yaml
copy .env.example .env
```

Проверка запуска в консоли (Ctrl+C — штатная остановка):

```bat
.venv\Scripts\python main.py
```

## Ключи OKX

**Демо (начинайте с него):**
1. okx.com → войти → переключатель **Demo trading** (вверху справа / в меню Trade).
2. В демо-режиме: профиль → **API** → **Create Demo Trading V5 API Key**.
3. Права: **Read + Trade**. **Withdraw НЕ включать.**
4. Задайте passphrase, сохраните key / secret / passphrase в `.env`:
   `OKX_DEMO_API_KEY`, `OKX_DEMO_API_SECRET`, `OKX_DEMO_API_PASSPHRASE`.
5. Демо-счёт пополняется виртуальными средствами в интерфейсе демо-торговли.

**Реальный счёт:** профиль → API → Create API key, права **Read + Trade, без Withdraw**.
Привязку к IP включайте **только если у вас статический IP** (иначе бот перестанет работать
при смене адреса). Ключи — в `OKX_API_KEY/SECRET/PASSPHRASE`, см. [чеклист](#чеклист-перехода-с-демо-на-реал).

Счёт OKX должен быть в режиме, где доступна спот-торговля (Spot / Spot & futures / Multi-currency —
любой). Ключи хранятся **только** в `.env`; логгер маскирует их значения, `.env` в `.gitignore`.

## Telegram-бот

1. В Telegram откройте **@BotFather** → `/newbot` → получите токен → `TELEGRAM_BOT_TOKEN` в `.env`.
2. Узнайте свой chat_id: напишите своему боту любое сообщение, затем откройте
   `https://api.telegram.org/bot<TOKEN>/getUpdates` и возьмите `message.chat.id`
   (или используйте @userinfobot). Запишите в `TELEGRAM_CHAT_ID`.
3. Бот отвечает **только** этому chat_id, сообщения из других чатов игнорируются и логируются.

## Конфигурация

Полный пример с комментариями — [config.example.yaml](config.example.yaml). Любой отсутствующий
ключ берётся из значений по умолчанию (`bot/config.py`). Ключевые блоки:

```yaml
symbols: [BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT]

alerts:
  price_move: [{pct: 2.0, minutes: 15}, {pct: 4.0, minutes: 60}]
  rsi: {enabled: true, period: 14, overbought: 75, oversold: 25}
  volume_spike: {enabled: true, timeframe: 5m, multiplier: 3.0, lookback: 20}
  cooldown_minutes: {default: 60, price_move: 30, levels: 120, rsi: 240, volume_spike: 60, ema_cross: 720}

trader:
  enabled: false
  exchange_side_sl_tp: true
  strategy: {ema_fast: 50, ema_slow: 200, rsi_period: 14, rsi_pullback: 35, rsi_max_entry: 60}

session_defaults:            # используются, если не заданы в /session_start
  budget_usdt: 10
  duration_hours: 8
  max_loss_usdt: 2
  profit_target_usdt: 1.5
  profit_lock: false
  profit_lock_trail_pct: 30
  max_trades: 10
  max_consecutive_losses: 3
  consecutive_loss_action: pause   # pause | stop
  on_session_end: close_all        # close_all | manage_only
  manage_only_max_minutes: 60
  stop_loss_pct: 2
  take_profit_pct: 4

hard_caps:                   # Telegram НЕ может их превысить
  max_budget_usdt: 50
  max_loss_usdt: 10
  max_duration_hours: 24

daily_caps:
  daily_budget_cap_usdt: 30
  daily_loss_cap_usdt: 5

schedule:                    # сессия каждый день в 00:00 на 8 часов
  enabled: true
  start_time: "00:00"
  days: [mon, tue, wed, thu, fri, sat, sun]
  params: {budget_usdt: 10, duration_hours: 8, max_loss_usdt: 2}
```

**Пример «осторожный»:** `budget_usdt: 10, max_loss_usdt: 1, profit_target_usdt: 1, max_trades: 4,
max_consecutive_losses: 2, consecutive_loss_action: stop`.
**Пример «дать прибыли бежать»:** `profit_target_usdt: 1, profit_lock: true, profit_lock_trail_pct: 30,
on_session_end: manage_only, manage_only_max_minutes: 120`.

Claude-разбор дайджеста: `analytics.llm.enabled: true`, ключ `ANTHROPIC_API_KEY` в `.env`.
Модель (`model`), цены за 1M токенов (`price_input_per_mtok`/`price_output_per_mtok`) и
дневной лимит (`daily_budget_usd`) — в конфиге. Перед запросом бот оценивает худший случай
стоимости и пропускает вызов, если лимит дня будет превышен; фактическая стоимость считается по
`usage` ответа. По умолчанию включён серверный fallback при отказе модели (`refusal_fallback`).

## Сессии и лимиты

Торговля идёт только внутри активной сессии.

* **Старт** — `/session_start` или по расписанию. Перед стартом проверяется: параметры в пределах
  `hard_caps`; **реальный свободный USDT на бирже ≥ бюджета**; `daily_budget_cap_usdt` (сумма бюджетов
  всех сессий за день); дневной лимит убытка ещё не достигнут; trader включён; нет файла `STOP`;
  реальный счёт подтверждён. Иначе сессия не стартует, причина уходит в Telegram.
* **Бюджет** — бот никогда не использует больше `budget_usdt`, даже если на счёте больше.
  Прибыль не реинвестируется сверх бюджета, убытки уменьшают доступный капитал.
  Позиция ≤ `position_size_pct` (≤ 30%) бюджета, ≤ 2 позиции, ≤ 1 на монету.
* **PnL сессии** = реализованный + нереализованный, **с учётом комиссий**: комиссия входа (OKX
  берёт её в базовой валюте при покупке — учитывается через фактически полученное количество) и
  оценка комиссии выхода в нереализованном PnL. Лимиты проверяются **на каждом тике** (событие
  тикера или раз в `tick_interval_sec`), а не только после закрытия сделки.
* **Остановки:**

| Условие | Действие |
|---|---|
| `max_loss_usdt` / `max_loss_pct` (берётся меньший, и никогда не больше `hard_caps.max_loss_usdt`) | закрыть всё по рынку, завершить |
| `daily_loss_cap_usdt` (все сессии за день + текущая) | закрыть всё, завершить |
| `profit_target_usdt` / `_pct`, `profit_lock: false` | закрыть всё, зафиксировать прибыль |
| цель + `profit_lock: true` | продолжить, включить трейлинг: стоп, если PnL упал на `profit_lock_trail_pct`% от пика |
| истекло `duration_hours` | по `on_session_end`: `close_all` — закрыть всё; `manage_only` — новые не открывать, текущие до SL/TP, но не дольше `manage_only_max_minutes`, затем закрыть |
| `max_trades` | новые не открывать; когда позиции закрылись — завершить |
| `max_consecutive_losses` | `pause` — пауза `consecutive_loss_pause_minutes`; `stop` — завершить по `on_session_end` |
| `/session_end` | по `on_session_end` |
| `/stop` или файл `STOP` | закрыть всё немедленно, trader выключить |

* **Отчёт** в конце каждой сессии: причина остановки (время / лимит убытка / цель / трейлинг /
  ручная / аварийная …), сделки, PnL с комиссиями, комиссии, винрейт, макс. просадка, пик PnL.
* **hard_caps** — максимальный бюджет, убыток, длительность (+ число сделок, размер позиции,
  число позиций). `/session_set` и `/session_start` отклоняют значения выше. Поднять потолки можно
  только правкой `config.yaml` и перезапуском.

## Команды Telegram

| Команда | Описание |
|---|---|
| `/session_start budget=10 hours=8 max_loss=2 target=1.5` | запустить сессию; недостающее — из `session_defaults`. Доп. ключи: `max_loss_pct`, `target_pct`, `lock=on`, `trail=30`, `trades=10`, `losses=3`, `end=manage_only`, `sl=2`, `tp=4` |
| `/session_status` | оставшееся время, бюджет (в позициях / доступно), PnL (реализ./нереализ./комиссии), расстояние до лимита убытка и цели, сделки |
| `/session_set <параметр> <значение>` | изменить на лету (в пределах `hard_caps`), напр. `/session_set max_loss 1.5`, `/session_set hours 6`, `/session_set lock on` |
| `/session_end` | завершить сессию по `on_session_end` |
| `/status`, `/pnl`, `/positions` | состояние, PnL за день/7д/30д, открытые позиции |
| `/pause`, `/resume` | пауза/возобновление новых входов (позиции ведутся) |
| `/stop` | **аварийно**: закрыть всё по рынку и выключить trader |
| `/trader_on`, `/trader_off` | включить/выключить trader |
| `/confirm_live <код>` | подтверждение реального счёта |
| `/digest`, `/weekly`, `/mute <мин>`, `/health`, `/help` | отчёты, тишина алертов, heartbeat |

Файл **`STOP`** в папке проекта = `/stop` (работает, даже если Telegram недоступен). Пока файл
существует, trader не включится. Файл `SHUTDOWN` (его создаёт `stop.bat`) — штатная остановка
процесса **без** закрытия позиций.

## Надёжность

* **Heartbeat**: каждый компонент (WS, тики, trader, telegram) отмечается; если какой-то молчит
  > 5 мин — алерт в Telegram. Плюс внешний `scripts/watchdog.py` (Task Scheduler, каждые 5 мин)
  читает `data/heartbeat.json` и шлёт алерт напрямую через Telegram API, **даже если процесс бота умер**.
* **Перезапуск / сон**: состояние сессии (время старта, бюджет, PnL, пик, позиции, счётчики)
  сохраняется в SQLite на каждое изменение и каждые 15 с. После рестарта или пробуждения бот:
  восстанавливает активную сессию (таймер считается от исходного старта), ищет «подвисшие»
  ордера по `clOrdId`, сверяет каждую позицию с реальным балансом (если OCO сработал, пока бот
  не работал — сделка закрывается по фактическим fill'ам), сверяет USDT. **Новая сессия
  автоматически не стартует** (слот расписания, пропущенный во время сна/простоя, не догоняется).
  Сон определяется по «скачку» времени в event loop, после чего WS переподключается и делается backfill.
* **Устаревшие данные**: нет тиков > 60 с → новые входы на паузе + уведомление; для лимитов и SL
  цены позиций берутся через REST.
* **Ошибки API**: ретраи с экспоненциальной паузой на сетевые ошибки и rate limit (ccxt
  `enableRateLimit`), ордера не повторяются вслепую — сначала поиск по `clOrdId`.
* **Логи**: `logs/bot.log` и `logs/errors.log` с ротацией (10 МБ × 10), секреты маскируются.
* **Один экземпляр**: `data/bot.lock` не даёт запустить два бота одновременно.

## Запуск 24/7 на Windows

Коротко (подробно, с отключением сна и NSSM — [docs/WINDOWS.md](docs/WINDOWS.md)):

```powershell
# отключить сон от сети
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
# автозапуск при входе + перезапуск + watchdog
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
Start-ScheduledTask -TaskName OKXBot
```

* `start.bat` — запустить в фоне (без окна), с автоперезапуском при падении.
* `stop.bat` — штатно остановить (сессия сохранится и восстановится при старте).

## Бэктест

```bat
.venv\Scripts\python backtest.py --days 180 --compare
.venv\Scripts\python backtest.py --days 365 --compare budget=10 hours=8 max_loss=1 target=1 lock=on trail=30
.venv\Scripts\python backtest.py --symbols BTC/USDT ETH/USDT --start-hour 9 end=manage_only --trades-csv data\trades.csv
```

История 1h качается с OKX (публичный API, без ключей) и кэшируется в `data/backtest/`.
Используются **тот же** `RiskEngine` и та же стратегия, что и в живой торговле: сессии стартуют
каждый день в `--start-hour` (по умолчанию из `schedule.start_time`), действуют дневные лимиты.
SL/TP — внутри бара по high/low (если задеты оба — пессимистично считается SL), лимиты убытка
проверяются по минимумам бара, затем по закрытию; комиссия — `exchange.taker_fee`, проскальзывание
`--slippage`. `--compare` дополнительно прогоняет стратегию без лимитов сессии (только SL/TP, 24/7) —
так видно, как лимиты влияют на результат (PnL, просадку, число сделок, причины остановок).

## Тесты

```bat
.venv\Scripts\python -m pytest -q
```

* `tests/test_risk.py` — лимит убытка (в т.ч. по нереализованному PnL на тике), учёт комиссий,
  цель прибыли и profit-lock/трейлинг, таймер (`close_all`/`manage_only`), `hard_caps`, лимиты входа
  (30%, 2 позиции, бюджет), серия убытков, дневной лимит, восстановление состояния после рестарта.
* `tests/test_trader.py` — трейдер с фейковой биржей: отказ при нехватке баланса, полный цикл
  с SL и отчётом, закрытие всего по лимиту на тике, файл STOP, `/session_set` против `hard_caps`,
  восстановление после рестарта (OCO сработал во время простоя, подвисший ордер, просроченная сессия
  не продлевается и новая не стартует), дневной лимит бюджета.
* `tests/test_indicators_strategy.py`, `tests/test_backtest.py` — индикаторы, сигнал, разбор WS-сообщений,
  алерты и cooldown, бэктест на синтетике.

## Чеклист перехода с демо на реал

1. ☐ Бэктест на 6–12 мес. с вашими лимитами (`--compare`) — результат понятен и приемлем.
2. ☐ Минимум 2–4 недели на DEMO: сессии стартуют/заканчиваются как ожидается, отчёты корректны,
   `/stop` и файл `STOP` проверены, рестарт и сон ПК проверены (сессия восстанавливается).
3. ☐ Реальный API-ключ: **Read + Trade, без Withdraw**; IP-привязка только при статическом IP.
4. ☐ На счёте лишь та сумма, которую вы готовы потерять; `hard_caps` и `daily_caps` выставлены
   **маленькими** (например, `max_budget_usdt: 20`, `max_loss_usdt: 3`, `daily_loss_cap_usdt: 3`).
5. ☐ `session_defaults` проверены, `schedule.enabled` — осознанно.
6. ☐ В `.env`: `OKX_API_KEY/SECRET/PASSPHRASE` и `OKX_LIVE_TRADING=true`. Перезапуск.
7. ☐ Бот пришлёт код → `/confirm_live <код>` (подтверждение привязано к отпечатку ключа; смена ключа
   требует нового подтверждения). Без него торговля заблокирована.
8. ☐ `/trader_on`, затем первая сессия с минимальным бюджетом; убедитесь, что OCO-ордера появились
   в интерфейсе OKX («Алгоритмические ордера»).
9. ☐ Сон ПК отключён, Task Scheduler/NSSM и watchdog работают, алерт «бот молчит» проверен
   (остановите бота на 6+ минут).
10. ☐ Откат на демо: `OKX_LIVE_TRADING=false` + перезапуск.

## Замечания

* Рыночные данные берутся с основного OKX, а ордера в DEMO исполняются по стакану демо-среды —
  цены могут немного отличаться.
* OCO на бирже резервирует проданное количество; перед ручным выходом бот снимает OCO.
* Код протестирован юнит- и интеграционными тестами с фейковой биржей и локальным WS-сервером;
  прогоните его на DEMO перед любыми реальными деньгами.
