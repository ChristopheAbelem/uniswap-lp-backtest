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

CREATE TABLE IF NOT EXISTS gas_prices (
    block_number     INTEGER PRIMARY KEY,
    block_time       INTEGER NOT NULL,
    base_fee_wei     INTEGER,  -- NULL before the London upgrade
    median_price_wei INTEGER   -- NULL when the block has no transactions
);

CREATE TABLE IF NOT EXISTS fee_protocol_changes (
    block_number  INTEGER NOT NULL,
    log_index     INTEGER NOT NULL,
    tx_hash       TEXT    NOT NULL,
    block_time    INTEGER NOT NULL,
    fee0_old      INTEGER NOT NULL,
    fee1_old      INTEGER NOT NULL,
    fee0_new      INTEGER NOT NULL,
    fee1_new      INTEGER NOT NULL,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS tx_gas (
    op           TEXT    NOT NULL,  -- mint, burn or swap
    tx_hash      TEXT    NOT NULL,
    block_number INTEGER NOT NULL,
    gas_used     INTEGER NOT NULL,
    PRIMARY KEY (op, tx_hash)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    event      TEXT    PRIMARY KEY,
    last_block INTEGER NOT NULL
);
"""

EVENT_TABLES = (
    "swaps",
    "mints",
    "burns",
    "collects",
    "gas_prices",
    "fee_protocol_changes",
    "tx_gas",
)


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


def load_swaps(conn, start_time, end_time):
    """Yield swaps of a time range for the engine, in chain order."""
    # gas_prices is small, so it finds the first block quickly.
    row = conn.execute(
        """
        SELECT MAX(block_number) FROM gas_prices WHERE block_time <= ?
        """,
        (start_time,),
    ).fetchone()
    first_block = row[0] or 0

    rows = conn.execute(
        """
        SELECT amount0, amount1, sqrt_price_x96, liquidity, tick, block_number, block_time
        FROM swaps
        WHERE block_number >= ?
        ORDER BY block_number, log_index
        """,
        (first_block,),
    )
    for *swap, block_time in rows:
        if block_time >= end_time:
            break
        if block_time >= start_time:
            yield swap


def load_fee_changes(conn):
    """Return [(block_number, protocol_share)] of the pool's fee changes."""
    rows = conn.execute(
        """
        SELECT block_number, fee0_new, fee1_new
        FROM fee_protocol_changes
        ORDER BY block_number, log_index
        """
    ).fetchall()

    changes = []
    for block_number, fee0, fee1 in rows:
        if fee0 != fee1:
            raise ValueError(f"Different protocol fees per token at block {block_number}")
        changes.append((block_number, 1 / fee0 if fee0 else 0.0))
    return changes


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
