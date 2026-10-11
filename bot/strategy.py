"""Strategy: 4h EMA50/EMA200 trend filter + 1h RSI(14) pullback entry. Long only.

Pure function shared by the live trader and the backtester.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .indicators import ema, rsi


@dataclass
class StrategyParams:
    ema_fast: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_pullback: float = 35.0   # RSI должен опуститься ниже и вернуться выше этого уровня
    rsi_max_entry: float = 60.0  # не входить, если RSI уже высокий

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "StrategyParams":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if hasattr(cls, k)})


@dataclass
class Signal:
    symbol: str
    price: float
    reason: str
    ema_fast: float
    ema_slow: float
    rsi_prev: float
    rsi_now: float


def trend_up(closes_4h: Sequence[float], p: StrategyParams) -> tuple[bool, float | None, float | None]:
    if len(closes_4h) < p.ema_slow + 1:
        return False, None, None
    ef = ema(closes_4h, p.ema_fast)[-1]
    es = ema(closes_4h, p.ema_slow)[-1]
    if ef is None or es is None:
        return False, ef, es
    return (ef > es and closes_4h[-1] > es), ef, es


def generate_signal(symbol: str, closes_1h: Sequence[float], closes_4h: Sequence[float],
                    p: StrategyParams) -> Signal | None:
    """Evaluate on CLOSED candles only. Returns a long entry signal or None."""
    up, ef, es = trend_up(closes_4h, p)
    if not up or len(closes_1h) < p.rsi_period + 2:
        return None
    r = rsi(closes_1h, p.rsi_period)
    prev, now = r[-2], r[-1]
    if prev is None or now is None:
        return None
    if prev < p.rsi_pullback <= now <= p.rsi_max_entry:
        reason = (f"Тренд 4h вверх: EMA{p.ema_fast}={ef:.6g} > EMA{p.ema_slow}={es:.6g}; "
                  f"откат RSI{p.rsi_period} 1h: {prev:.1f} → {now:.1f} (пересечение {p.rsi_pullback:g} снизу)")
        return Signal(symbol, closes_1h[-1], reason, ef, es, prev, now)  # type: ignore[arg-type]
    return None
