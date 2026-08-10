#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pytest"]
# ///
# REUSE_CHECKED: none   No tests existed in this repo before this file. Searched
# the wider repos tree for an existing SQLite outbox or InfluxDB line protocol
# implementation to extend; every hit was either a vendored pip package or
# mission-control's unrelated email outbox, so this is new.
"""
Tests for the edge pipeline's two testable halves: the SQLite outbox and the
InfluxDB line protocol serializer.

    uv run test_yt98h_pipeline.py

Nothing here touches a real Modbus device or a real InfluxDB. Those are the two
boundaries of this pipeline and both are stubbed out by construction: the outbox
takes plain dicts, and the serializer returns a string that is never posted
anywhere. What that leaves is exactly the logic that can silently corrupt data
without anyone noticing, which is the part worth testing.

Covered:
    outbox         write, query unsent oldest first, batch limit, mark sent,
                   backlog count, durability across a reopen
    line protocol  integer field suffix, tags always strings, escaping,
                   timestamp precision and exactness
    retry          a row that fails to mark stays queryable, a row that
                   succeeds is never handed out again
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yt98h_lineprotocol as lp
import yt98h_outbox as outbox


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "outbox.sqlite3")


@pytest.fixture
def conn(db_path):
    c = outbox.connect(db_path)
    yield c
    c.close()


def reading(address=1, gas="H2S", value=0.0, raw=0,
            stamp="2026-08-10T12:00:00.000000+00:00"):
    """One outbox row's worth of collector output."""
    return {
        "site_id": "poll-01",
        "sensor_address": address,
        "gas_type": gas,
        "value": value,
        "raw_register_value": raw,
        "reading_timestamp_utc": stamp,
        "created_at_utc": stamp,
    }


# --------------------------------------------------------------------------
# Outbox: schema and writes
# --------------------------------------------------------------------------

def test_connect_creates_schema(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "readings" in names


def test_connect_creates_unsent_index(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_readings_unsent" in names


def test_connect_is_idempotent(db_path):
    # The collector and the forwarder each open their own connection to the
    # same file, and the container restarts on top of an existing outbox.
    # Applying the schema twice must not error or destroy anything.
    first = outbox.connect(db_path)
    outbox.insert_readings(first, [reading()])
    first.close()

    second = outbox.connect(db_path)
    assert outbox.count_unsent(second) == 1
    second.close()


def test_insert_writes_every_column(conn):
    outbox.insert_readings(conn, [reading(address=3, gas="O2", value=20.9, raw=209)])
    row = conn.execute("SELECT * FROM readings").fetchone()

    assert row["site_id"] == "poll-01"
    assert row["sensor_address"] == 3
    assert row["gas_type"] == "O2"
    assert row["value"] == pytest.approx(20.9)
    assert row["raw_register_value"] == 209
    assert row["reading_timestamp_utc"] == "2026-08-10T12:00:00.000000+00:00"
    assert row["created_at_utc"] == "2026-08-10T12:00:00.000000+00:00"


def test_insert_defaults_sent_to_zero(conn):
    outbox.insert_readings(conn, [reading()])
    assert conn.execute("SELECT sent FROM readings").fetchone()["sent"] == 0


def test_insert_commits_before_returning(conn, db_path):
    # This is the property the whole zero data loss requirement rests on: once
    # insert_readings returns, the reading survives losing the process. Reading
    # it back over a second, independent connection is the closest this can get
    # to pulling the plug.
    outbox.insert_readings(conn, [reading()])

    other = sqlite3.connect(db_path)
    try:
        assert other.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
    finally:
        other.close()


def test_insert_of_a_whole_poll_cycle(conn):
    cycle = [reading(address=a) for a in (1, 2, 3, 4, 5)]
    outbox.insert_readings(conn, cycle)
    assert outbox.count_unsent(conn) == 5


def test_insert_of_nothing_is_harmless(conn):
    # A poll cycle where every address failed to answer produces no rows.
    outbox.insert_readings(conn, [])
    assert outbox.count_unsent(conn) == 0


# --------------------------------------------------------------------------
# Outbox: the forwarder's query
# --------------------------------------------------------------------------

def test_fetch_unsent_returns_oldest_first(conn):
    for a in (1, 2, 3):
        outbox.insert_readings(conn, [reading(address=a)])

    rows = outbox.fetch_unsent(conn, limit=10)
    assert [r["sensor_address"] for r in rows] == [1, 2, 3]


def test_fetch_unsent_respects_the_batch_limit(conn):
    for a in range(1, 6):
        outbox.insert_readings(conn, [reading(address=a)])

    rows = outbox.fetch_unsent(conn, limit=2)
    assert len(rows) == 2
    assert [r["sensor_address"] for r in rows] == [1, 2]


def test_fetch_unsent_excludes_sent_rows(conn):
    for a in (1, 2, 3):
        outbox.insert_readings(conn, [reading(address=a)])
    first = outbox.fetch_unsent(conn, limit=1)

    outbox.mark_sent(conn, [first[0]["id"]])

    assert [r["sensor_address"] for r in outbox.fetch_unsent(conn, limit=10)] == [2, 3]


def test_fetch_unsent_on_an_empty_outbox(conn):
    assert outbox.fetch_unsent(conn, limit=10) == []


def test_count_unsent_tracks_the_backlog(conn):
    for a in range(1, 6):
        outbox.insert_readings(conn, [reading(address=a)])
    assert outbox.count_unsent(conn) == 5

    outbox.mark_sent(conn, [r["id"] for r in outbox.fetch_unsent(conn, limit=3)])
    assert outbox.count_unsent(conn) == 2


# --------------------------------------------------------------------------
# Retry and idempotency
# --------------------------------------------------------------------------

def test_a_row_that_was_never_marked_is_handed_out_again(conn):
    # The forwarder's failure path: the HTTP write did not return 204, so
    # nothing is marked, and the identical batch must come back next cycle.
    outbox.insert_readings(conn, [reading(address=a) for a in (1, 2, 3)])

    attempt = outbox.fetch_unsent(conn, limit=10)
    # Failure. mark_sent is deliberately not called.
    retry = outbox.fetch_unsent(conn, limit=10)

    assert [r["id"] for r in retry] == [r["id"] for r in attempt]


def test_a_row_that_succeeded_is_never_handed_out_again(conn):
    outbox.insert_readings(conn, [reading(address=a) for a in (1, 2, 3)])

    pushed = outbox.fetch_unsent(conn, limit=10)
    outbox.mark_sent(conn, [r["id"] for r in pushed])

    assert outbox.fetch_unsent(conn, limit=10) == []
    assert outbox.count_unsent(conn) == 0


def test_partial_success_leaves_only_the_unconfirmed_rows(conn):
    outbox.insert_readings(conn, [reading(address=a) for a in (1, 2, 3, 4)])
    rows = outbox.fetch_unsent(conn, limit=10)

    # Two confirmed, two not.
    outbox.mark_sent(conn, [rows[0]["id"], rows[1]["id"]])

    assert [r["id"] for r in outbox.fetch_unsent(conn, limit=10)] == [
        rows[2]["id"], rows[3]["id"]]


def test_mark_sent_survives_a_restart(conn, db_path):
    # A crash between "write succeeded" and "mark sent" must not resurrect
    # rows that were already confirmed.
    outbox.insert_readings(conn, [reading(address=a) for a in (1, 2)])
    outbox.mark_sent(conn, [r["id"] for r in outbox.fetch_unsent(conn, limit=10)])
    conn.close()

    reopened = outbox.connect(db_path)
    try:
        assert outbox.count_unsent(reopened) == 0
    finally:
        reopened.close()


def test_mark_sent_of_nothing_is_harmless(conn):
    outbox.insert_readings(conn, [reading()])
    outbox.mark_sent(conn, [])
    assert outbox.count_unsent(conn) == 1


def test_mark_sent_twice_is_a_no_op(conn):
    outbox.insert_readings(conn, [reading()])
    row_id = outbox.fetch_unsent(conn, limit=1)[0]["id"]

    outbox.mark_sent(conn, [row_id])
    outbox.mark_sent(conn, [row_id])

    assert outbox.count_unsent(conn) == 0


# --------------------------------------------------------------------------
# Line protocol: timestamps
# --------------------------------------------------------------------------

def test_iso_to_unix_nanos_is_exact():
    # 2026-08-10T12:00:00Z. Computed with integer arithmetic, not via a float
    # seconds value, because float64 cannot represent nanosecond resolution at
    # a 2026 epoch offset and would quietly round the low digits away.
    assert lp.iso_to_unix_nanos("2026-08-10T12:00:00+00:00") == 1786363200000000000


def test_iso_to_unix_nanos_keeps_sub_second_precision():
    stamp = "2026-08-10T12:00:00.123456+00:00"
    assert lp.iso_to_unix_nanos(stamp) == 1786363200123456000


def test_iso_to_unix_nanos_treats_a_naive_timestamp_as_utc():
    assert (lp.iso_to_unix_nanos("2026-08-10T12:00:00")
            == lp.iso_to_unix_nanos("2026-08-10T12:00:00+00:00"))


def test_iso_to_unix_nanos_honours_a_non_utc_offset():
    assert (lp.iso_to_unix_nanos("2026-08-10T14:00:00+02:00")
            == lp.iso_to_unix_nanos("2026-08-10T12:00:00+00:00"))


def test_two_readings_a_microsecond_apart_get_distinct_timestamps():
    # The collision failure mode in the spec: two points sharing a measurement,
    # tag set and timestamp are one point, and the second silently overwrites
    # the first. Poll cycles are 5 to 10 seconds apart, so microsecond
    # resolution is six orders of magnitude of headroom.
    a = lp.iso_to_unix_nanos("2026-08-10T12:00:00.000001+00:00")
    b = lp.iso_to_unix_nanos("2026-08-10T12:00:00.000002+00:00")
    assert b - a == 1000


# --------------------------------------------------------------------------
# Line protocol: point encoding
# --------------------------------------------------------------------------

def test_encode_point_full_line():
    line = lp.encode_point(reading(address=3, gas="O2", value=20.9, raw=209))
    assert line == (
        "gas_reading,gas_type=O2,sensor_address=3,site_id=poll-01"
        " value=20.9,raw_register_value=209i"
        " 1786363200000000000")


def test_encode_point_uses_the_documented_measurement():
    assert lp.encode_point(reading()).startswith("gas_reading,")


def test_integer_field_carries_the_i_suffix():
    # Without the suffix InfluxDB parses 209 as a float and the field's type in
    # the bucket silently stops matching the documented schema.
    line = lp.encode_point(reading(raw=209))
    assert "raw_register_value=209i" in line


def test_float_field_carries_no_suffix():
    line = lp.encode_point(reading(value=20.9))
    assert "value=20.9," in line


def test_a_whole_number_reading_still_serializes_as_a_float():
    # addr 2 (CO) has zero decimal places, so value is 0.0 or 834.0. It must
    # not collapse to "834" and land in the bucket as an integer field.
    line = lp.encode_point(reading(value=834.0, raw=834))
    assert "value=834.0," in line


def test_sensor_address_is_a_string_tag_not_a_number():
    # sensor_address is INTEGER in SQLite but line protocol has no integer tag
    # type, so it must be written bare with no i suffix and no quotes.
    line = lp.encode_point(reading(address=5))
    assert ",sensor_address=5," in line
    assert "sensor_address=5i" not in line
    assert 'sensor_address="5"' not in line


def test_tag_keys_are_sorted():
    # Sorted tag keys are InfluxDB's documented recommendation for write
    # performance, and they make the output stable enough to assert on.
    line = lp.encode_point(reading())
    tag_section = line.split(" ")[0]
    assert tag_section == "gas_reading,gas_type=H2S,sensor_address=1,site_id=poll-01"


def test_line_has_exactly_three_space_separated_sections():
    line = lp.encode_point(reading())
    assert len(line.split(" ")) == 3


def test_negative_values_encode():
    line = lp.encode_point(reading(value=-1.5, raw=-15))
    assert "value=-1.5," in line
    assert "raw_register_value=-15i" in line


# --------------------------------------------------------------------------
# Line protocol: escaping
# --------------------------------------------------------------------------

def test_space_in_a_tag_value_is_escaped():
    # An operator setting SITE_ID to "north poll" would otherwise terminate the
    # tag set early and produce a malformed point that InfluxDB rejects, which
    # in this pipeline means the backlog never drains again.
    row = reading()
    row["site_id"] = "north poll"
    assert "site_id=north\\ poll" in lp.encode_point(row)


def test_comma_in_a_tag_value_is_escaped():
    row = reading()
    row["site_id"] = "poll,01"
    assert "site_id=poll\\,01" in lp.encode_point(row)


def test_equals_in_a_tag_value_is_escaped():
    row = reading()
    row["site_id"] = "poll=01"
    assert "site_id=poll\\=01" in lp.encode_point(row)


def test_unmapped_gas_code_passes_through_unharmed():
    # yt98h_modbus renders an unknown r25 as e.g. "code7". Nothing to escape,
    # but it must not be dropped or mangled.
    row = reading(gas="code7")
    assert "gas_type=code7" in lp.encode_point(row)


# --------------------------------------------------------------------------
# Line protocol: batches
# --------------------------------------------------------------------------

def test_encode_batch_joins_with_newlines():
    body = lp.encode_batch([reading(address=1), reading(address=2)])
    assert len(body.split("\n")) == 2


def test_encode_batch_has_no_trailing_newline():
    body = lp.encode_batch([reading()])
    assert not body.endswith("\n")


def test_encode_batch_of_nothing_is_empty():
    assert lp.encode_batch([]) == ""


def test_encode_batch_accepts_sqlite_rows(conn):
    # The forwarder hands sqlite3.Row objects straight through, not dicts.
    outbox.insert_readings(conn, [reading(address=3, gas="O2", value=20.9, raw=209)])
    rows = outbox.fetch_unsent(conn, limit=10)

    assert lp.encode_batch(rows) == lp.encode_point(
        reading(address=3, gas="O2", value=20.9, raw=209))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
