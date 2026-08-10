#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
# REUSE_CHECKED: yt98h_modbus.py   The Modbus framing, the firmware quirk
# handling, the 32 register block read and the r31 / 10**r27 decode already
# exist and are bench verified. This imports read_channel, open_port and
# autodetect_port rather than reimplementing any of it, and changes nothing in
# that file. yt98h_outbox.py owns the SQLite side.
"""
Collector: poll every sensor address, write every reading to the outbox.

    uv run yt98h_collector.py

Step 1 and Step 2 of docs/architecture.md. One row per channel per poll cycle,
committed to SQLite before anything touches the network. This loop never blocks
on network state and knows nothing about InfluxDB; the forwarder is a separate
loop reading the same file.

Environment (see the Container Contract in docs/architecture.md):

    SITE_ID                 required, written into every row
    SQLITE_DB_PATH          required, the outbox file
    SERIAL_PORT             optional, auto-detected when unset
    MODBUS_BAUD             optional, defaults to 9600
    MODBUS_ADDRESSES        optional, defaults to 1,2,3,4,5
    POLL_INTERVAL_SECONDS   optional, defaults to 5
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

# yt98h_modbus.py stays at the repo root, one directory up, where the bench
# tooling lives and where the README documents it. This package reuses it rather
# than vendoring a second copy that would drift. Inside the container both files
# sit in /app together and this line is a harmless no-op.

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yt98h_modbus  # noqa: E402  repo root, see the sys.path line above
import yt98h_outbox as outbox  # noqa: E402

LOG = logging.getLogger("collector")

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_ADDRESSES = "1,2,3,4,5"


def require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit("%s is required. See the Container Contract in "
                 "docs/architecture.md." % name)
    return value


def config_from_env():
    return {
        "db_path": require_env("SQLITE_DB_PATH"),
        "site_id": require_env("SITE_ID"),
        "port": os.environ.get("SERIAL_PORT") or None,
        "baud": int(os.environ.get("MODBUS_BAUD", yt98h_modbus.BAUD)),
        "addresses": [int(part) for part
                      in os.environ.get("MODBUS_ADDRESSES", DEFAULT_ADDRESSES).split(",")
                      if part.strip()],
        "interval": float(os.environ.get("POLL_INTERVAL_SECONDS",
                                         DEFAULT_POLL_INTERVAL_SECONDS)),
    }


def poll_cycle(ser, addresses, baud, site_id):
    """Read every address once and return (readings, addresses that did not answer).

    The reading timestamp is taken per channel at the moment that channel is
    read, not once for the cycle, and at microsecond resolution. That precision
    is a structural requirement, not a detail: InfluxDB identifies a point by
    measurement, tag set and timestamp together, so two readings for the same
    channel that land on the same timestamp value are one point, and the second
    silently overwrites the first with no error. Cycles are seconds apart and
    microseconds are six orders of magnitude finer, so the collision cannot
    happen by accident.

    A channel that does not answer is skipped rather than written as a zero. A
    missing row is a gap; a fabricated zero is a false gas reading.
    """
    readings = []
    silent = []
    created_at = None

    for address in addresses:
        channel = yt98h_modbus.read_channel(ser, address, baud)
        if channel is None:
            silent.append(address)
            continue
        readings.append({
            "site_id": site_id,
            "sensor_address": address,
            "gas_type": channel["gas"],
            "value": channel["value"],
            "raw_register_value": channel["raw_value"],
            "reading_timestamp_utc": datetime.now(timezone.utc).isoformat(
                timespec="microseconds"),
            "created_at_utc": None,
        })

    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    for row in readings:
        row["created_at_utc"] = created_at

    return readings, silent


def run(db_path, site_id, port, baud, addresses, interval, stop_event):
    """Poll forever, or until stop_event is set.

    Opens its own SQLite connection: the forwarder runs in a separate thread
    with its own, and sqlite3 connections are not shared across threads.

    The serial port is opened lazily and reopened on any failure. A USB RS-485
    adapter that is unplugged, re-enumerated under a different device node, or
    briefly wedged must cost one poll cycle, not the life of the container.
    """
    conn = outbox.connect(db_path)
    ser = None
    LOG.info("started: site=%s addresses=%s interval=%.1fs outbox=%s",
             site_id, addresses, interval, db_path)

    try:
        while not stop_event.is_set():
            cycle_started = time.monotonic()
            try:
                if ser is None:
                    resolved = yt98h_modbus.autodetect_port(port)
                    ser = yt98h_modbus.open_port(resolved, baud, yt98h_modbus.PARITY)
                    LOG.info("serial port open: %s at %d baud", resolved, baud)

                readings, silent = poll_cycle(ser, addresses, baud, site_id)
                outbox.insert_readings(conn, readings)

                if silent:
                    LOG.warning("poll ok: %d readings written, no response from %s",
                                len(readings), silent)
                else:
                    LOG.info("poll ok: %d readings written", len(readings))

            except (SystemExit, Exception) as exc:
                # SystemExit is caught deliberately: yt98h_modbus.autodetect_port
                # exits when it finds no serial ports at all, which is the
                # ordinary state of the box while the adapter is unplugged. In a
                # container that must be a retryable cycle failure, not a dead
                # process.
                LOG.error("poll failed: %s", exc)
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None

            elapsed = time.monotonic() - cycle_started
            stop_event.wait(max(0.0, interval - elapsed))
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        conn.close()
        LOG.info("stopped")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    stop_event = threading.Event()
    try:
        run(stop_event=stop_event, **config_from_env())
    except KeyboardInterrupt:
        stop_event.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
