"""Strategy: 4h EMA50/EMA200 trend filter + 1h entry. Long only.

Pure functions shared by the live trader and the backtester.

Entry modes:
* ``pullback`` (default): 1h RSI(14) crosses ``rsi_pullback`` from below inside a 4h uptrend.
* ``breakout``: 1h close above the highest high of the previous ``breakout_bars`` candles
  inside a 4h uptrend (RSI below ``rsi_max_breakout`` to skip exhausted moves).

Optional filters/exits (all off by default = the original behaviour):
* ``exit_mode: atr`` — SL/TP distances from 1h ATR so that the targets match the
  6–8 h horizon of a session. The stop is never wider than the session ``stop_loss_pct``.
* ``min_atr_pct`` — skip entries when 1h ATR% is too small to pay the round-trip costs.
* ``btc_regime`` — trade alts only while BTC itself is in a 4h uptrend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .indicators import ema, rsi

ENTRY_MODES = ("pullback", "breakout")
EXIT_MODES = ("fixed", "atr")


@dataclass
class StrategyParams:
    ema_fast: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_pullback: float = 35.0   # RSI должен опуститься ниже и вернуться выше этого уровня
    rsi_max_entry: float = 60.0  # не входить, если RSI уже высокий
    entry_mode: str = "pullback"
    breakout_bars: int = 20
    rsi_max_breakout: float = 75.0
    exit_mode: str = "fixed"
    atr_period: int = 14         # on 1h candles
    atr_sl_mult: float = 2.0
    atr_tp_mult: float = 3.0
    min_atr_pct: float = 0.0     # 0 = off
    btc_regime: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "StrategyParams":
        d = d or {}
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Неизвестные параметры strategy: {', '.join(sorted(unknown))}")
        out = cls()
        for k, v in d.items():
            cur = getattr(out, k)
            if isinstance(cur, bool):
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            setattr(out, k, type(cur)(v))
        if out.entry_mode not in ENTRY_MODES:
            raise ValueError(f"strategy.entry_mode: одно из {ENTRY_MODES}")
        if out.exit_mode not in EXIT_MODES:
            raise ValueError(f"strategy.exit_mode: одно из {EXIT_MODES}")
        return out


@dataclass
class Signal:
    symbol: str
    price: float
    reason: str
    ema_fast: float
    ema_slow: float
    rsi_prev: float
    rsi_now: float
    sl_pct: float | None = None   # None -> session stop_loss_pct / take_profit_pct
    tp_pct: float | None = None
    atr_pct: float | None = None


def trend_up(closes_4h: Sequence[float], p: StrategyParams) -> tuple[bool, float | None, float | None]:
    if len(closes_4h) < p.ema_slow + 1:
        return False, None, None
    ef = ema(closes_4h, p.ema_fast)[-1]
    es = ema(closes_4h, p.ema_slow)[-1]
    if ef is None or es is None:
        return False, ef, es
    return (ef > es and closes_4h[-1] > es), ef, es


def atr_pct_1h(candles: Sequence[Sequence[float]], period: int) -> float | None:
    """Wilder ATR of [ts, o, h, l, c, v] candles, in % of the last close."""
    if len(candles) < period + 1:
        return None
    trs = [max(c[2] - c[3], abs(c[2] - prev[4]), abs(c[3] - prev[4])) for prev, c in zip(candles, candles[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr / candles[-1][4] * 100 if candles[-1][4] else None


def generate_signal(symbol: str, closes_1h: Sequence[float], closes_4h: Sequence[float],
                    p: StrategyParams, candles_1h: Sequence[Sequence[float]] | None = None,
                    btc_closes_4h: Sequence[float] | None = None,
                    fee_rate: float = 0.001) -> Signal | None:
    """Evaluate on CLOSED candles only. Returns a long entry signal or None.

    ``candles_1h`` (full OHLCV rows) is needed for the breakout entry, ATR exits and the
    ATR filter; ``btc_closes_4h`` for the BTC regime filter."""
    if len(closes_1h) < p.rsi_period + 2:
        return None
    # cheap 1h trigger first (most bars have none), the 4h trend only when needed
    r = rsi(closes_1h, p.rsi_period)
    prev, now = r[-2], r[-1]
    if prev is None or now is None:
        return None
    if p.entry_mode == "pullback":
        if not (prev < p.rsi_pullback <= now <= p.rsi_max_entry):
            return None
        trigger = f"откат RSI{p.rsi_period} 1h: {prev:.1f} → {now:.1f} (пересечение {p.rsi_pullback:g} снизу)"
    else:
        if candles_1h is None or len(candles_1h) < p.breakout_bars + 1:
            return None
        level = max(c[2] for c in candles_1h[-p.breakout_bars - 1:-1])
        if not (closes_1h[-1] > level and now < p.rsi_max_breakout):
            return None
        trigger = f"пробой максимума {p.breakout_bars} ч ({level:.6g}), RSI {now:.1f}"
    up, ef, es = trend_up(closes_4h, p)
    if not up:
        return None
    if p.btc_regime and not symbol.startswith("BTC/"):
        if btc_closes_4h is None or not trend_up(btc_closes_4h, p)[0]:
            return None
    atr = None
    sl_pct = tp_pct = None
    if p.exit_mode == "atr" or p.min_atr_pct > 0:
        if candles_1h is None:
            return None
        atr = atr_pct_1h(candles_1h[-(p.atr_period * 10):], p.atr_period)
        if atr is None or atr < p.min_atr_pct:
            return None
    if p.exit_mode == "atr":
        assert atr is not None
        sl_pct = p.atr_sl_mult * atr        # capped by the session stop_loss_pct in the caller
        tp_pct = max(p.atr_tp_mult * atr, 3 * 2 * fee_rate * 100 + 0.01)
    reason = (f"Тренд 4h вверх: EMA{p.ema_fast}={ef:.6g} > EMA{p.ema_slow}={es:.6g}; {trigger}"
              + (f"; ATR1h {atr:.2f}%" if atr is not None else "")
              + ("; BTC в аптренде" if p.btc_regime else ""))
    return Signal(symbol, closes_1h[-1], reason, ef, es, prev, now,  # type: ignore[arg-type]
                  sl_pct=sl_pct, tp_pct=tp_pct, atr_pct=atr)


def exit_levels(entry: float, sig: Signal | None, stop_loss_pct: float, take_profit_pct: float) -> tuple[float, float]:
    """Stop/target prices. ATR-based distances never exceed the session stop_loss_pct."""
    sl = stop_loss_pct if sig is None or sig.sl_pct is None else min(sig.sl_pct, stop_loss_pct)
    tp = take_profit_pct if sig is None or sig.tp_pct is None else sig.tp_pct
    return entry * (1 - sl / 100), entry * (1 + tp / 100)
