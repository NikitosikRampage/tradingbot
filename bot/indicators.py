"""Technical indicators on plain Python lists (no pandas dependency)."""
from __future__ import annotations

import math
from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """EMA seeded with SMA of the first ``period`` values."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_val(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_val(avg_g, avg_l)
    return out


def _rsi_val(g: float, l: float) -> float:
    if l == 0:
        return 100.0 if g > 0 else 50.0
    rs = g / l
    return 100.0 - 100.0 / (1.0 + rs)


def pct_change(a: float, b: float) -> float:
    return (b - a) / a * 100.0 if a else 0.0


def log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]


def stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return cov / math.sqrt(va * vb)


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def resample(candles: Sequence[Sequence[float]], src_tf: str, dst_tf: str,
             drop_incomplete: bool = True) -> list[list[float]]:
    """Aggregate [ts_ms, o, h, l, c, v] candles into a bigger, UTC-aligned timeframe."""
    step = TF_SECONDS[dst_tf] * 1000
    src = TF_SECONDS[src_tf] * 1000
    need = step // src
    out: list[list[float]] = []
    counts: list[int] = []
    for c in candles:
        bucket = int(c[0]) // step * step
        if out and out[-1][0] == bucket:
            o = out[-1]
            o[2] = max(o[2], c[2])
            o[3] = min(o[3], c[3])
            o[4] = c[4]
            o[5] += c[5]
            counts[-1] += 1
        else:
            out.append([bucket, c[1], c[2], c[3], c[4], c[5]])
            counts.append(1)
    if drop_incomplete and out and counts[-1] < need:
        out.pop()
    return out
