"""SQLite storage.

The universe table is deliberately append-only, keyed by snapshot
timestamp. Every refresh writes a new generation rather than overwriting
the last one. That costs a few MB a day and buys two things that are
otherwise expensive: screen membership diffs ("what entered my screen and
why"), and genuine point-in-time history, which is the only honest way to
backtest a screen without look-ahead bias. Vendors charge real money for
point-in-time fundamentals; accumulating our own from day one is free.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from . import config

# Columns of the universe snapshot. Order matters only for readability;
# everything is addressed by name.
UNIVERSE_COLUMNS = [
    ("symbol", "TEXT"), ("name", "TEXT"),
    ("sector", "TEXT"), ("industry", "TEXT"), ("country", "TEXT"),
    ("exchange", "TEXT"), ("quote_type", "TEXT"), ("currency", "TEXT"),
    ("ipo_year", "INTEGER"),
    ("price", "REAL"), ("change", "REAL"), ("change_pct", "REAL"),
    ("open", "REAL"), ("prev_close", "REAL"),
    ("day_high", "REAL"), ("day_low", "REAL"),
    ("volume", "REAL"), ("avg_volume_3m", "REAL"), ("avg_volume_10d", "REAL"),
    ("market_cap", "REAL"), ("shares_outstanding", "REAL"),
    ("pe", "REAL"), ("forward_pe", "REAL"), ("pb", "REAL"),
    ("eps_ttm", "REAL"), ("eps_forward", "REAL"), ("book_value", "REAL"),
    ("dividend_yield", "REAL"), ("dividend_rate", "REAL"),
    ("week52_high", "REAL"), ("week52_low", "REAL"),
    ("week52_change_pct", "REAL"),
    ("sma50", "REAL"), ("sma200", "REAL"),
    ("analyst_rating", "REAL"), ("earnings_ts", "REAL"),
    ("market_state", "TEXT"), ("lag_seconds", "REAL"),
    # Extended hours. `price` above is always the regular-session print;
    # these are the pre- and after-hours moves against it, kept separate so
    # a screen can ask for either without the two contaminating each other.
    ("pre_price", "REAL"), ("pre_change_pct", "REAL"), ("pre_time", "REAL"),
    ("post_price", "REAL"), ("post_change_pct", "REAL"), ("post_time", "REAL"),
    # Whichever of the two is live now, resolved at write time.
    ("ext_price", "REAL"), ("ext_change_pct", "REAL"), ("ext_label", "TEXT"),
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS universe (
    snapshot_ts REAL NOT NULL,
    {", ".join(f"{n} {t}" for n, t in UNIVERSE_COLUMNS)},
    PRIMARY KEY (snapshot_ts, symbol)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_universe_symbol ON universe(symbol);
CREATE INDEX IF NOT EXISTS idx_universe_ts ON universe(snapshot_ts);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_ts REAL PRIMARY KEY,
    rows INTEGER NOT NULL,
    elapsed REAL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS screens (
    name TEXT PRIMARY KEY,
    expr TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS screen_runs (
    name TEXT NOT NULL,
    run_ts REAL NOT NULL,
    symbols TEXT NOT NULL,
    PRIMARY KEY (name, run_ts)
);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    shares REAL NOT NULL,
    cost_basis REAL,
    opened_at REAL,
    note TEXT
);
"""

_local = threading.local()


def _migrate(c: sqlite3.Connection) -> list[str]:
    """Add columns that postdate an existing database.

    `CREATE TABLE IF NOT EXISTS` silently does nothing when the table is
    already there, so a new field in UNIVERSE_COLUMNS would otherwise blow
    up every insert against a database built before it. Rebuilding the
    7,000-row snapshot to add a column would also throw away the
    point-in-time history, which is the one thing here that can't be
    re-fetched.
    """
    have = {r["name"] for r in c.execute("PRAGMA table_info(universe)")}
    if not have:
        return []
    added = []
    for name, kind in UNIVERSE_COLUMNS:
        if name not in have:
            c.execute(f"ALTER TABLE universe ADD COLUMN {name} {kind}")
            added.append(name)
    if added:
        c.commit()
    return added


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.db_path(), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        _migrate(c)
        _local.conn = c
    return c


# ---- universe snapshots ----

def write_snapshot(rows: list[dict], elapsed: float = 0.0,
                   note: str = "") -> float:
    """Append a generation. Returns its timestamp."""
    ts = time.time()
    names = [n for n, _ in UNIVERSE_COLUMNS]
    sql = (f"INSERT OR REPLACE INTO universe (snapshot_ts, {', '.join(names)}) "
           f"VALUES ({', '.join(['?'] * (len(names) + 1))})")
    c = conn()
    c.executemany(sql, [[ts] + [r.get(n) for n in names] for r in rows])
    c.execute("INSERT OR REPLACE INTO snapshots (snapshot_ts, rows, elapsed, note)"
              " VALUES (?,?,?,?)", (ts, len(rows), elapsed, note))
    c.commit()
    return ts


def latest_snapshot_ts() -> float | None:
    row = conn().execute(
        "SELECT snapshot_ts FROM snapshots ORDER BY snapshot_ts DESC LIMIT 1"
    ).fetchone()
    return row["snapshot_ts"] if row else None


def snapshot_meta(ts: float | None = None) -> dict | None:
    ts = ts or latest_snapshot_ts()
    if ts is None:
        return None
    row = conn().execute("SELECT * FROM snapshots WHERE snapshot_ts = ?",
                         (ts,)).fetchone()
    return dict(row) if row else None


def read_snapshot(ts: float | None = None) -> list[dict]:
    ts = ts or latest_snapshot_ts()
    if ts is None:
        return []
    rows = conn().execute("SELECT * FROM universe WHERE snapshot_ts = ?",
                          (ts,)).fetchall()
    return [dict(r) for r in rows]


def snapshot_list(limit: int = 60) -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM snapshots ORDER BY snapshot_ts DESC LIMIT ?",
        (limit,)).fetchall()]


def prune_snapshots(keep: int = 90) -> int:
    """Keep the most recent N generations. Point-in-time history is
    valuable but not unbounded."""
    keep_ts = [r["snapshot_ts"] for r in conn().execute(
        "SELECT snapshot_ts FROM snapshots ORDER BY snapshot_ts DESC LIMIT ?",
        (keep,)).fetchall()]
    if not keep_ts:
        return 0
    c = conn()
    placeholders = ",".join("?" * len(keep_ts))
    cur = c.execute(
        f"DELETE FROM universe WHERE snapshot_ts NOT IN ({placeholders})", keep_ts)
    c.execute(
        f"DELETE FROM snapshots WHERE snapshot_ts NOT IN ({placeholders})", keep_ts)
    c.commit()
    return cur.rowcount


# ---- saved screens ----

def save_screen(name: str, expr: str) -> None:
    now = time.time()
    conn().execute(
        "INSERT INTO screens (name, expr, created_at, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET expr=excluded.expr, updated_at=excluded.updated_at",
        (name, expr, now, now))
    conn().commit()


def list_screens() -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM screens ORDER BY updated_at DESC").fetchall()]


def get_screen(name: str) -> dict | None:
    row = conn().execute("SELECT * FROM screens WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def delete_screen(name: str) -> None:
    conn().execute("DELETE FROM screens WHERE name = ?", (name,))
    conn().execute("DELETE FROM screen_runs WHERE name = ?", (name,))
    conn().commit()


def record_run(name: str, symbols: list[str]) -> float:
    ts = time.time()
    conn().execute("INSERT OR REPLACE INTO screen_runs (name, run_ts, symbols)"
                   " VALUES (?,?,?)", (name, ts, json.dumps(sorted(symbols))))
    conn().commit()
    return ts


def last_runs(name: str, limit: int = 2) -> list[dict]:
    rows = conn().execute(
        "SELECT run_ts, symbols FROM screen_runs WHERE name = ?"
        " ORDER BY run_ts DESC LIMIT ?", (name, limit)).fetchall()
    return [{"run_ts": r["run_ts"], "symbols": json.loads(r["symbols"])}
            for r in rows]


# ---- positions ----

def upsert_position(symbol: str, shares: float, cost_basis: float | None = None,
                    note: str = "") -> None:
    conn().execute(
        "INSERT INTO positions (symbol, shares, cost_basis, opened_at, note)"
        " VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET"
        " shares=excluded.shares, cost_basis=excluded.cost_basis, note=excluded.note",
        (symbol.upper(), shares, cost_basis, time.time(), note))
    conn().commit()


def positions() -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM positions ORDER BY symbol").fetchall()]


def delete_position(symbol: str) -> None:
    conn().execute("DELETE FROM positions WHERE symbol = ?", (symbol.upper(),))
    conn().commit()
