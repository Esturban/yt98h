#!/usr/bin/env python3
# REUSE_CHECKED: none   Searched the repos tree for an existing SQLite outbox or
# store-and-forward queue to extend; every hit was a vendored pip package or
# mission-control's unrelated email outbox. Schema is defined by
# docs/architecture.md, "Local Outbox Schema (SQLite)".
"""
The SQLite outbox: the only thing standing between a reading and data loss.

Stdlib only, no dependencies, no PEP 723 block, because this is imported by
yt98h_collector.py and yt98h_forwarder.py rather than run on its own. Keeping it
dependency free is also what lets the test suite exercise it without pyserial.

The one property that matters here is durability. Once insert_readings returns,
the reading has survived commit, and no later failure in this pipeline, network,
process, or power, can turn it back into a lost reading. Everything downstream
of that commit can only add delay.
"""

import sqlite3

# Straight from docs/architecture.md. The index leads on `sent` to serve the
# forwarder's filter and trails on `id` to serve its ordering, so the only query
# this table ever answers, "sent = 0, oldest first", is answered entirely from
# the index instead of degrading into a scan once an outage has left tens of
# thousands of rows behind.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id               TEXT    NOT NULL,
    sensor_address        INTEGER NOT NULL,
    gas_type              TEXT    NOT NULL,
    value                 REAL    NOT NULL,
    raw_register_value    INTEGER NOT NULL,
    reading_timestamp_utc TEXT    NOT NULL,
    sent                  INTEGER NOT NULL DEFAULT 0,
    created_at_utc        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_unsent ON readings (sent, id);
"""

INSERT_SQL = """
INSERT INTO readings (
    site_id, sensor_address, gas_type, value, raw_register_value,
    reading_timestamp_utc, created_at_utc
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

COLUMNS = ("site_id", "sensor_address", "gas_type", "value",
           "raw_register_value", "reading_timestamp_utc", "created_at_utc")


def connect(db_path):
    """Open the outbox at db_path, creating the file and schema if needed.

    Safe to call repeatedly and against an existing outbox: the container
    restarts on top of whatever backlog is already on disk, and the collector
    and forwarder each open their own connection to the same file.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # WAL lets the forwarder read while the collector writes, so a slow drain
    # can never block a poll cycle. synchronous=FULL is the deliberate cost:
    # every commit is flushed to disk before it returns, which is exactly the
    # guarantee the zero data loss requirement is asking for. At one row per
    # second this costs nothing worth measuring.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def insert_readings(conn, readings):
    """Write one poll cycle's readings and commit before returning.

    Each reading is a mapping carrying the seven collector-supplied columns in
    COLUMNS. `id` and `sent` are left to the schema. The whole cycle goes in one
    transaction: either the cycle is in the outbox or it is not, which is the
    behaviour the reboot failure mode in the spec assumes.
    """
    rows = [tuple(r[c] for c in COLUMNS) for r in readings]
    if not rows:
        return
    conn.executemany(INSERT_SQL, rows)
    conn.commit()


def fetch_unsent(conn, limit):
    """The forwarder's only query: unsent rows, oldest first, at most `limit`.

    `limit` is FORWARDER_BATCH_SIZE. It is what keeps a backlog of a quarter of
    a million rows from becoming one enormous HTTP request.
    """
    return conn.execute(
        "SELECT * FROM readings WHERE sent = 0 ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def mark_sent(conn, row_ids):
    """Mark rows as sent. Called only after a confirmed InfluxDB write.

    Nothing else in this pipeline advances a row's state, so a row is unsent
    until InfluxDB has actually accepted it. Marking an already sent row is a
    no-op, which is what makes a crash between the write and the mark harmless.
    """
    ids = list(row_ids)
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        "UPDATE readings SET sent = 1 WHERE id IN (%s)" % placeholders, ids)
    conn.commit()


def count_unsent(conn):
    """Current backlog size. Logged every forwarder cycle, and the single
    number that reveals a backlog growing faster than it drains."""
    return conn.execute(
        "SELECT COUNT(*) FROM readings WHERE sent = 0").fetchone()[0]
