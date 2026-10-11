"""Helpers to read series from the DB."""
from __future__ import annotations

from .db import Database
from .indicators import resample


def closes(db: Database, symbol: str, tf: str, limit: int) -> list[float]:
    return [r[4] for r in db.candles(symbol, tf, limit=limit)]


def candles_4h(db: Database, symbol: str, limit: int = 260) -> list[list[float]]:
    """4h candles built from closed 1h candles (UTC-aligned: 00, 04, 08, ...)."""
    rows = db.candles(symbol, "1h", limit=limit * 4 + 8)
    out = resample(rows, "1h", "4h", drop_incomplete=True)
    return out[-limit:]


def closes_4h(db: Database, symbol: str, limit: int = 260) -> list[float]:
    return [r[4] for r in candles_4h(db, symbol, limit)]
