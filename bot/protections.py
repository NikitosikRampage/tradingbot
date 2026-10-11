"""Entry protections modelled on Freqtrade's plugins (CooldownPeriod, StoplossGuard,
LowProfitPairs, MaxDrawdown).

Pure and stateless: locks are recomputed from the closed-trade history, so they survive
restarts and work the same way in the live trader and in the backtester. Protections can
only BLOCK new entries — they never open trades or loosen any session limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class ClosedTrade:
    symbol: str
    exit_ts: float
    pnl_usdt: float
    pnl_pct: float
    exit_kind: str = "other"   # stop_loss | take_profit | trailing_stop | time_limit | session | other


STOPLOSS_KINDS = ("stop_loss", "trailing_stop")


@dataclass
class ProtectionConfig:
    enabled: bool = False
    # CooldownPeriod: lock the pair for N minutes after any exit
    cooldown_minutes: float = 0.0
    # StoplossGuard: >= trade_limit losing stop-loss exits within lookback -> lock everything
    stoploss_guard: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False, "lookback_hours": 24, "trade_limit": 2, "stop_minutes": 360, "only_per_pair": False})
    # LowProfitPairs: pair profit over lookback below required_profit_pct -> lock the pair
    low_profit_pairs: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False, "lookback_hours": 72, "trade_limit": 2, "required_profit_pct": 0.0, "stop_minutes": 1440})
    # MaxDrawdown (equity mode): realised drawdown within lookback > max_drawdown_usdt -> lock everything
    max_drawdown: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False, "lookback_hours": 72, "trade_limit": 3, "max_drawdown_usdt": 1.5, "stop_minutes": 720})

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ProtectionConfig":
        d = dict(d or {})
        base = cls()
        for k in ("stoploss_guard", "low_profit_pairs", "max_drawdown"):
            merged = dict(getattr(base, k))
            merged.update(d.pop(k, None) or {})
            setattr(base, k, merged)
        if "enabled" in d:
            base.enabled = bool(d.pop("enabled"))
        if "cooldown_minutes" in d:
            base.cooldown_minutes = float(d.pop("cooldown_minutes"))
        if d:
            raise ValueError(f"Неизвестные параметры protections: {', '.join(sorted(d))}")
        return base

    @property
    def max_lookback_sec(self) -> float:
        hours = [float(self.stoploss_guard["lookback_hours"]) + float(self.stoploss_guard["stop_minutes"]) / 60,
                 float(self.low_profit_pairs["lookback_hours"]) + float(self.low_profit_pairs["stop_minutes"]) / 60,
                 float(self.max_drawdown["lookback_hours"]) + float(self.max_drawdown["stop_minutes"]) / 60,
                 self.cooldown_minutes / 60]
        return max(hours) * 3600


@dataclass
class Lock:
    until: float
    reason: str
    symbol: str | None   # None = all pairs


def _window(trades: list[ClosedTrade], end: float, lookback_sec: float) -> list[ClosedTrade]:
    return [t for t in trades if end - lookback_sec <= t.exit_ts <= end]


def compute_locks(trades: Iterable[ClosedTrade], cfg: ProtectionConfig, now: float) -> list[Lock]:
    """All locks that are active at ``now``. Each protection is evaluated at every past exit
    (like Freqtrade, which evaluates protections after each closed trade)."""
    if not cfg.enabled:
        return []
    ts = sorted((t for t in trades if t.exit_ts <= now), key=lambda t: t.exit_ts)
    locks: list[Lock] = []

    if cfg.cooldown_minutes > 0:
        last: dict[str, float] = {}
        for t in ts:
            last[t.symbol] = t.exit_ts
        for sym, ex in last.items():
            until = ex + cfg.cooldown_minutes * 60
            if until > now:
                locks.append(Lock(until, f"CooldownPeriod {sym}", sym))

    sg = cfg.stoploss_guard
    if sg.get("enabled"):
        lb, lim, stop = float(sg["lookback_hours"]) * 3600, int(sg["trade_limit"]), float(sg["stop_minutes"]) * 60
        for t in ts:
            if t.exit_ts + stop <= now:
                continue
            win = [x for x in _window(ts, t.exit_ts, lb) if x.exit_kind in STOPLOSS_KINDS and x.pnl_usdt < 0]
            if sg.get("only_per_pair"):
                win = [x for x in win if x.symbol == t.symbol]
            if len(win) >= lim:
                sym = t.symbol if sg.get("only_per_pair") else None
                locks.append(Lock(t.exit_ts + stop,
                                  f"StoplossGuard {sym or 'все пары'}: {len(win)} SL за {sg['lookback_hours']}ч",
                                  sym))

    lp = cfg.low_profit_pairs
    if lp.get("enabled"):
        lb, lim, stop = float(lp["lookback_hours"]) * 3600, int(lp["trade_limit"]), float(lp["stop_minutes"]) * 60
        req = float(lp["required_profit_pct"])
        for t in ts:
            if t.exit_ts + stop <= now:
                continue
            win = [x for x in _window(ts, t.exit_ts, lb) if x.symbol == t.symbol]
            if len(win) >= lim:
                profit = sum(x.pnl_pct for x in win)
                if profit < req:
                    locks.append(Lock(t.exit_ts + stop, f"LowProfitPairs {t.symbol}: {profit:+.2f}%", t.symbol))

    md = cfg.max_drawdown
    if md.get("enabled"):
        lb, lim, stop = float(md["lookback_hours"]) * 3600, int(md["trade_limit"]), float(md["stop_minutes"]) * 60
        limit = float(md["max_drawdown_usdt"])
        for t in ts:
            if t.exit_ts + stop <= now:
                continue
            win = _window(ts, t.exit_ts, lb)
            if len(win) < lim:
                continue
            eq = peak = dd = 0.0
            for x in win:
                eq += x.pnl_usdt
                peak = max(peak, eq)
                dd = max(dd, peak - eq)
            if dd > limit:
                locks.append(Lock(t.exit_ts + stop, f"MaxDrawdown: {dd:.2f} USDT за {md['lookback_hours']}ч", None))
    return locks


def entry_blocked(trades: Iterable[ClosedTrade], cfg: ProtectionConfig, symbol: str,
                  now: float) -> tuple[bool, str]:
    hits = [lk for lk in compute_locks(trades, cfg, now) if lk.symbol is None or lk.symbol == symbol]
    if not hits:
        return False, ""
    lk = max(hits, key=lambda x: x.until)
    return True, lk.reason
