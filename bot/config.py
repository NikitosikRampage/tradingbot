"""Configuration loading: config.yaml (+ built-in defaults) and secrets from .env."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from .risk import DailyCaps, HardCaps, RiskEngine, SessionParams
from .strategy import StrategyParams

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


DEFAULTS: dict[str, Any] = {
    "timezone": "Europe/Moscow",
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"],
    "paths": {"db": "data/bot.db", "logs": "logs", "stop_file": "STOP", "shutdown_file": "SHUTDOWN",
              "heartbeat_file": "data/heartbeat.json"},
    "logging": {"level": "INFO", "max_bytes": 10_000_000, "backup_count": 10},
    "exchange": {
        "ws_public": "wss://ws.okx.com:8443/ws/v5/public",
        "ws_business": "wss://ws.okx.com:8443/ws/v5/business",
        "taker_fee": 0.001,
        "request_timeout_ms": 20000,
    },
    "collector": {
        "timeframes": ["1m", "5m", "1h"],
        "history": {"1m": 1440, "5m": 864, "1h": 1000},
        "retention_days": {"1m": 7, "5m": 30},
        "stale_after_sec": 60,
        "ticker_flush_sec": 10,
        "ping_interval_sec": 20,
    },
    "alerts": {
        "enabled": True,
        "price_move": [{"pct": 2.0, "minutes": 15}, {"pct": 4.0, "minutes": 60}],
        "levels": {"enabled": True, "windows": ["24h", "7d"], "touch_pct": 0.3, "bounce_pct": 0.8},
        "rsi": {"enabled": True, "period": 14, "overbought": 75, "oversold": 25},
        "volume_spike": {"enabled": True, "timeframe": "5m", "multiplier": 3.0, "lookback": 20},
        "ema_cross": {"enabled": True, "fast": 50, "slow": 200},
        "cooldown_minutes": {"default": 60, "price_move": 30, "levels": 120, "rsi": 240,
                             "volume_spike": 60, "ema_cross": 720},
    },
    "analytics": {
        "digest_enabled": True,
        "digest_time": "09:00",
        "weekly_enabled": True,
        "weekly_day": "sun",
        "weekly_time": "20:00",
        "llm": {
            "enabled": False,
            "model": "claude-opus-5-5",
            "effort": "low",
            "max_tokens": 2000,
            "daily_budget_usd": 0.50,
            "price_input_per_mtok": 4.0,
            "price_output_per_mtok": 20.0,
            "refusal_fallback": True,
        },
    },
    "trader": {
        "enabled": False,
        "exchange_side_sl_tp": True,
        "tick_interval_sec": 1.0,
        # entry/exit accepted by scripts/research.py (2019-2022 dev, 2023-now holdout; see IMPROVEMENTS.md)
        "strategy": {"ema_fast": 50, "ema_slow": 200, "rsi_period": 14,
                     "rsi_pullback": 35, "rsi_max_entry": 60,
                     "entry_mode": "breakout", "breakout_bars": 20,
                     "exit_mode": "atr", "atr_sl_mult": 2.0, "atr_tp_mult": 3.0, "btc_regime": True},
        "protections": {"enabled": False},
        # accepted by scripts/evaluate_improvements.py on 2y of OKX history (see IMPROVEMENTS.md)
        "exits": {"time_limit_hours": 6},
    },
    "pairlist": {"enabled": False},
    "session_defaults": {**SessionParams().to_dict(), "no_entry_last_hours": 4},
    "hard_caps": HardCaps().__dict__,
    "daily_caps": DailyCaps().__dict__,
    "schedule": {"enabled": False, "start_time": "00:00",
                 "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                 "grace_minutes": 15, "params": {}},
    "health": {"silent_alert_minutes": 5, "check_interval_sec": 30},
    "telegram": {"enabled": True},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("session_defaults", "hard_caps"):
            out[k] = deep_merge(out[k], v)
        elif k in ("session_defaults",) and isinstance(v, dict):
            merged = copy.deepcopy(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Secrets:
    okx_key: str = ""
    okx_secret: str = ""
    okx_passphrase: str = ""
    live_trading: bool = False
    telegram_token: str = ""
    telegram_chat_id: int | None = None
    anthropic_key: str = ""

    def values(self) -> list[str]:
        return [v for v in (self.okx_key, self.okx_secret, self.okx_passphrase,
                            self.telegram_token, self.anthropic_key) if v]


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def load_secrets(env_file: Path | None = None) -> Secrets:
    load_dotenv(env_file or ROOT / ".env", override=False)
    live = _truthy(os.getenv("OKX_LIVE_TRADING"))
    prefix = "OKX_" if live else "OKX_DEMO_"
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return Secrets(
        okx_key=os.getenv(prefix + "API_KEY", ""),
        okx_secret=os.getenv(prefix + "API_SECRET", ""),
        okx_passphrase=os.getenv(prefix + "API_PASSPHRASE", ""),
        live_trading=live,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=int(chat) if chat.lstrip("-").isdigit() else None,
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path
    tz: ZoneInfo
    symbols: list[str]
    session_defaults: SessionParams
    hard_caps: HardCaps
    daily_caps: DailyCaps
    secrets: Secrets = field(default_factory=Secrets)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.root / p

    @property
    def taker_fee(self) -> float:
        return float(self.raw["exchange"]["taker_fee"])

    def risk_engine(self) -> RiskEngine:
        return RiskEngine(self.hard_caps, self.daily_caps, self.taker_fee)


def load_config(path: Path | str | None = None, root: Path | None = None,
                with_secrets: bool = True) -> Config:
    root = root or ROOT
    path = Path(path) if path else root / "config.yaml"
    user: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
    raw = deep_merge(DEFAULTS, user)
    try:
        tz = ZoneInfo(raw["timezone"])
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Неверный timezone: {raw['timezone']} ({exc}). На Windows: pip install tzdata") from exc
    try:
        caps = HardCaps.from_dict(raw["hard_caps"])
        daily = DailyCaps.from_dict(raw["daily_caps"])
        defaults = SessionParams.from_dict(raw["session_defaults"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
    errs = RiskEngine(caps, daily).validate_params(defaults)
    if errs:
        raise ConfigError("session_defaults нарушают hard_caps: " + "; ".join(errs))
    if raw["schedule"].get("enabled"):
        try:
            sched = defaults.replace(**(raw["schedule"].get("params") or {}))
        except ValueError as exc:
            raise ConfigError(f"schedule.params: {exc}") from exc
        errs = RiskEngine(caps, daily).validate_params(sched)
        if errs:
            raise ConfigError("schedule.params нарушают hard_caps: " + "; ".join(errs))
    from .exits import ExitConfig
    from .pairlist import PairlistConfig
    from .protections import ProtectionConfig
    try:
        ProtectionConfig.from_dict(raw["trader"].get("protections"))
        StrategyParams.from_dict(raw["trader"].get("strategy"))
        ExitConfig.from_dict(raw["trader"].get("exits"))
        PairlistConfig.from_dict(raw.get("pairlist"))
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
    symbols = [s.upper() for s in raw["symbols"]]
    for s in symbols:
        if not s.endswith("/USDT"):
            raise ConfigError(f"Поддерживаются только спот-пары к USDT: {s}")
    cfg = Config(raw=raw, root=root, tz=tz, symbols=symbols, session_defaults=defaults,
                 hard_caps=caps, daily_caps=daily)
    if with_secrets:
        cfg.secrets = load_secrets(root / ".env")
    return cfg
