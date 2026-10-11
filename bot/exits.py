"""Position exit barriers (Hummingbot "triple barrier" idea): stop-loss, take-profit,
optional trailing stop / break-even and an optional time limit.

Pure functions shared by the live trader and the backtester. The stop can only move UP
(never loosened), so these options can only reduce the risk of an open position.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .risk import Position


@dataclass
class ExitConfig:
    trailing_enabled: bool = False
    trailing_activation_pct: float = 2.0   # trailing starts once price is +X% above entry
    trailing_distance_pct: float = 1.5     # stop = highest * (1 - distance)
    breakeven_enabled: bool = False
    breakeven_activation_pct: float = 1.5  # at +X% move the stop to entry + round-trip fees
    time_limit_hours: float = 0.0          # 0 = off; close positions older than this

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ExitConfig":
        d = d or {}
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Неизвестные параметры exits: {', '.join(sorted(unknown))}")
        return cls(**{k: type(getattr(cls(), k))(v) for k, v in d.items()})

    @property
    def active(self) -> bool:
        return self.trailing_enabled or self.breakeven_enabled or self.time_limit_hours > 0


def raise_stop(pos: Position, high: float, cfg: ExitConfig, fee_rate: float) -> bool:
    """Update ``pos.highest`` with a new high and raise the stop if configured.
    Returns True if the stop moved up."""
    pos.highest = max(pos.highest or pos.entry_price, high)
    new_stop = pos.stop_loss
    gain = (pos.highest / pos.entry_price - 1) * 100
    if cfg.breakeven_enabled and gain >= cfg.breakeven_activation_pct:
        new_stop = max(new_stop, pos.entry_price * (1 + 2.5 * fee_rate))
    if cfg.trailing_enabled and gain >= cfg.trailing_activation_pct:
        new_stop = max(new_stop, pos.highest * (1 - cfg.trailing_distance_pct / 100))
    new_stop = min(new_stop, pos.take_profit * 0.999)  # never cross the TP
    if new_stop > pos.stop_loss:
        pos.stop_loss = new_stop
        return True
    return False


def stop_kind(pos: Position) -> str:
    return "trailing_stop" if pos.initial_stop and pos.stop_loss > pos.initial_stop * 1.0000001 else "stop_loss"


def check_exit(pos: Position, price: float, now: float, cfg: ExitConfig) -> tuple[str, str] | None:
    """Return (exit_kind, human reason) or None."""
    if price <= pos.stop_loss:
        kind = stop_kind(pos)
        name = "трейлинг-стоп" if kind == "trailing_stop" else "стоп-лосс"
        return kind, f"{name} ({price:.6g} ≤ {pos.stop_loss:.6g})"
    if price >= pos.take_profit:
        return "take_profit", f"тейк-профит ({price:.6g} ≥ {pos.take_profit:.6g})"
    if cfg.time_limit_hours > 0 and now - pos.opened_at >= cfg.time_limit_hours * 3600:
        return "time_limit", f"лимит времени удержания {cfg.time_limit_hours:g}ч"
    return None
