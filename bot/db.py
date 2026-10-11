"""SQLite storage (WAL mode). Single connection, used from one asyncio thread."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL, confirmed INTEGER DEFAULT 1,
    PRIMARY KEY (symbol, tf, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS tickers (
    symbol TEXT PRIMARY KEY, ts INTEGER, last REAL, bid REAL, ask REAL,
    high24 REAL, low24 REAL, open24 REAL, vol24 REAL
);

CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, rule TEXT, symbol TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts ON alerts_log(rule, symbol, ts);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL, ends_at REAL, ended_at REAL,
    status TEXT, stop_reason TEXT, mode TEXT,
    budget_usdt REAL, params_json TEXT, state_json TEXT,
    realized_pnl REAL DEFAULT 0, fees REAL DEFAULT 0, trades INTEGER DEFAULT 0,
    max_drawdown REAL DEFAULT 0, report TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER, symbol TEXT, side TEXT DEFAULT 'long', mode TEXT,
    status TEXT,              -- pending | open | closed | failed
    client_order_id TEXT, order_id TEXT, algo_id TEXT,
    entry_ts REAL, entry_price REAL, qty REAL, cost_usdt REAL, entry_fee_usdt REAL,
    stop_loss REAL, take_profit REAL, entry_reason TEXT,
    exit_ts REAL, exit_price REAL, proceeds_usdt REAL, exit_fee_usdt REAL, exit_reason TEXT,
    pnl_usdt REAL, pnl_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_session ON trades(session_id);
CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades(exit_ts);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, json.dumps(value), time.time()))

    def kv_del(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key=?", (key,))

    # ------------------------------------------------------------------ candles
    def upsert_candles(self, symbol: str, tf: str, rows: list[list[float]], confirmed: bool = True) -> None:
        if not rows:
            return
        self.executemany(
            "INSERT INTO candles(symbol, tf, ts, o, h, l, c, v, confirmed) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, tf, ts) DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l, "
            "c=excluded.c, v=excluded.v, confirmed=excluded.confirmed",
            [(symbol, tf, int(r[0]), r[1], r[2], r[3], r[4], r[5] or 0.0, int(confirmed)) for r in rows])

    def candles(self, symbol: str, tf: str, limit: int = 500, since_ms: int | None = None,
                confirmed_only: bool = True) -> list[list[float]]:
        cond = "symbol=? AND tf=?"
        args: list[Any] = [symbol, tf]
        if since_ms is not None:
            cond += " AND ts>=?"
            args.append(since_ms)
        if confirmed_only:
            cond += " AND confirmed=1"
        rows = self.query(f"SELECT ts,o,h,l,c,v FROM (SELECT * FROM candles WHERE {cond} "
                          f"ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC", (*args, limit))
        return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]

    def candle_at_or_after(self, symbol: str, tf: str, ts_ms: int) -> list[float] | None:
        r = self.one("SELECT ts,o,h,l,c,v FROM candles WHERE symbol=? AND tf=? AND ts>=? ORDER BY ts ASC LIMIT 1",
                     (symbol, tf, ts_ms))
        return [r[0], r[1], r[2], r[3], r[4], r[5]] if r else None

    def last_candle_ts(self, symbol: str, tf: str) -> int | None:
        row = self.one("SELECT MAX(ts) AS t FROM candles WHERE symbol=? AND tf=? AND confirmed=1",
                       (symbol, tf))
        return int(row["t"]) if row and row["t"] is not None else None

    def prune_candles(self, tf: str, older_than_ms: int) -> None:
        self.execute("DELETE FROM candles WHERE tf=? AND ts<?", (tf, older_than_ms))

    # ------------------------------------------------------------------ tickers
    def save_tickers(self, tickers: dict[str, dict[str, Any]]) -> None:
        self.executemany(
            "INSERT OR REPLACE INTO tickers(symbol, ts, last, bid, ask, high24, low24, open24, vol24) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(s, t.get("ts"), t.get("last"), t.get("bid"), t.get("ask"), t.get("high24"),
              t.get("low24"), t.get("open24"), t.get("vol24")) for s, t in tickers.items()])

    # ------------------------------------------------------------------ alerts
    def log_alert(self, rule: str, symbol: str, message: str) -> None:
        self.execute("INSERT INTO alerts_log(ts, rule, symbol, message) VALUES(?,?,?,?)",
                     (int(time.time()), rule, symbol, message))

    # ------------------------------------------------------------------ sessions
    def create_session(self, started_at: float, ends_at: float, mode: str, budget: float,
                       params: dict[str, Any]) -> int:
        cur = self.execute(
            "INSERT INTO sessions(started_at, ends_at, status, mode, budget_usdt, params_json) "
            "VALUES(?,?,?,?,?,?)", (started_at, ends_at, "active", mode, budget, json.dumps(params)))
        return int(cur.lastrowid)

    def save_session_state(self, state_dict: dict[str, Any]) -> None:
        self.execute(
            "UPDATE sessions SET state_json=?, status=?, stop_reason=?, ends_at=?, ended_at=?, "
            "realized_pnl=?, fees=?, trades=?, max_drawdown=?, params_json=?, budget_usdt=? WHERE id=?",
            (json.dumps(state_dict), state_dict["status"], state_dict.get("stop_reason"),
             state_dict["ends_at"], state_dict.get("ended_at"), state_dict["realized_pnl"],
             state_dict["fees_paid"], state_dict["trades_opened"], state_dict["max_drawdown"],
             json.dumps(state_dict["params"]), state_dict["params"]["budget_usdt"],
             state_dict["session_id"]))

    def set_session_report(self, session_id: int, report: str) -> None:
        self.execute("UPDATE sessions SET report=? WHERE id=?", (report, session_id))

    def active_session_row(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sessions WHERE status!='ended' ORDER BY id DESC LIMIT 1")

    def sessions_since(self, ts: float) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sessions WHERE started_at>=? ORDER BY id", (ts,))

    # ------------------------------------------------------------------ trades
    def insert_trade(self, **cols: Any) -> int:
        keys = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        cur = self.execute(f"INSERT INTO trades({keys}) VALUES({qs})", cols.values())
        return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **cols: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        self.execute(f"UPDATE trades SET {sets} WHERE id=?", (*cols.values(), trade_id))

    def trades(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.query(f"SELECT * FROM trades WHERE {where} ORDER BY id", params)

    def realized_pnl_between(self, start: float, end: float, exclude_session: int | None = None) -> float:
        sql = "SELECT COALESCE(SUM(pnl_usdt),0) AS s FROM trades WHERE status='closed' AND exit_ts>=? AND exit_ts<?"
        args: list[Any] = [start, end]
        if exclude_session is not None:
            sql += " AND session_id!=?"
            args.append(exclude_session)
        row = self.one(sql, args)
        return float(row["s"]) if row else 0.0
