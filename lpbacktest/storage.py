"""SQLite storage for raw Uniswap v3 pool events."""

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data/pool_events.sqlite")

# uint160, uint128 and int256 values do not fit SQLite's 64-bit INTEGER,
# so they are stored as TEXT and converted back with int().
SCHEMA = """
CREATE TABLE IF NOT EXISTS swaps (
    block_number   INTEGER NOT NULL,
    log_index      INTEGER NOT NULL,
    tx_hash        TEXT    NOT NULL,
    block_time     INTEGER NOT NULL,
    sender         TEXT    NOT NULL,
    recipient      TEXT    NOT NULL,
    amount0        TEXT    NOT NULL,
    amount1        TEXT    NOT NULL,
    sqrt_price_x96 TEXT    NOT NULL,
    liquidity      TEXT    NOT NULL,
    tick           INTEGER NOT NULL,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS mints (
    block_number INTEGER NOT NULL,
    log_index    INTEGER NOT NULL,
    tx_hash      TEXT    NOT NULL,
    block_time   INTEGER NOT NULL,
    sender       TEXT    NOT NULL,
    owner        TEXT    NOT NULL,
    tick_lower   INTEGER NOT NULL,
    tick_upper   INTEGER NOT NULL,
    liquidity    TEXT    NOT NULL,
    amount0      TEXT    NOT NULL,
    amount1      TEXT    NOT NULL,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS burns (
    block_number INTEGER NOT NULL,
    log_index    INTEGER NOT NULL,
    tx_hash      TEXT    NOT NULL,
    block_time   INTEGER NOT NULL,
    owner        TEXT    NOT NULL,
    tick_lower   INTEGER NOT NULL,
    tick_upper   INTEGER NOT NULL,
    liquidity    TEXT    NOT NULL,
    amount0      TEXT    NOT NULL,
    amount1      TEXT    NOT NULL,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS collects (
    block_number INTEGER NOT NULL,
    log_index    INTEGER NOT NULL,
    tx_hash      TEXT    NOT NULL,
    block_time   INTEGER NOT NULL,
    owner        TEXT    NOT NULL,
    recipient    TEXT    NOT NULL,
    tick_lower   INTEGER NOT NULL,
    tick_upper   INTEGER NOT NULL,
    amount0      TEXT    NOT NULL,
    amount1      TEXT    NOT NULL,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    event      TEXT    PRIMARY KEY,
    last_block INTEGER NOT NULL
);
"""

EVENT_TABLES = ("swaps", "mints", "burns", "collects")


def connect(db_path=DEFAULT_DB_PATH):
    """Open the database and create the tables if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def insert_events(conn, table, rows):
    """Insert event dicts, ignoring rows already stored. Returns rows added."""
    if table not in EVENT_TABLES:
        raise ValueError(f"Unknown event table: {table}")
    if not rows:
        return 0

    columns = list(rows[0])
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({placeholders})"
    values = [tuple(row[column] for column in columns) for row in rows]

    with conn:
        before = conn.total_changes
        conn.executemany(sql, values)
        return conn.total_changes - before


def get_checkpoint(conn, event):
    """Return the last block saved for an event, or None."""
    row = conn.execute(
        "SELECT last_block FROM checkpoints WHERE event = ?", (event,)
    ).fetchone()
    return row[0] if row else None


def set_checkpoint(conn, event, last_block):
    """Save the last block fully collected for an event."""
    with conn:
        conn.execute(
            """
            INSERT INTO checkpoints (event, last_block) VALUES (?, ?)
            ON CONFLICT(event) DO UPDATE SET last_block = excluded.last_block
            """,
            (event, last_block),
        )
