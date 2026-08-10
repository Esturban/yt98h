#!/usr/bin/env python3
# REUSE_CHECKED: yt98h_outbox.py, yt98h_lineprotocol.py   The queue operations
# and the point serialization already exist and are tested; this file is only
# the loop and the HTTP call. urllib is used rather than requests or
# influxdb-client so this stays stdlib only and adds nothing to the container.
"""
Forwarder: push unsent outbox rows to the command-center InfluxDB.

    uv run yt98h_forwarder.py

Step 3 of docs/architecture.md, and it is best effort by design. The link is
intermittent, unscheduled, and treated as opaque: this loop does not probe it,
does not alert on downtime, and does not change behaviour based on how long it
has been down. It tries, and if it fails it tries again next cycle.

The single rule that makes that safe is that success is the only thing that
advances state. A row is marked sent only after InfluxDB has returned 204. Any
failure, of any kind, leaves every row untouched. A crash between the write
succeeding and the mark landing is harmless: the retry writes the same
measurement, tag set and timestamp with identical field values, which InfluxDB
treats as an overwrite of the same point, not a duplicate record.

Environment (see the Container Contract in docs/architecture.md):

    SQLITE_DB_PATH                      required, the outbox file
    INFLUXDB_URL                        required, e.g. http://command-center:8086
    INFLUXDB_TOKEN                      required, write-scoped token
    INFLUXDB_ORG                        required
    INFLUXDB_BUCKET                     required
    FORWARDER_RETRY_INTERVAL_SECONDS    optional, defaults to 15
    FORWARDER_BATCH_SIZE                optional, defaults to 500
"""

import logging
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import yt98h_lineprotocol as lineprotocol
import yt98h_outbox as outbox

LOG = logging.getLogger("forwarder")

DEFAULT_RETRY_INTERVAL_SECONDS = 15.0
DEFAULT_BATCH_SIZE = 500

# Not an environment variable: it is not in the Container Contract, and a write
# that has not completed in half a minute over this link is a failed cycle by
# any useful definition. Without it, urllib would block the loop indefinitely on
# a half-open connection, which is precisely what a marginal link produces.
HTTP_TIMEOUT_SECONDS = 30

# InfluxDB's write API returns 204 No Content on success. Nothing else counts.
# Treating any other 2xx as success would risk marking rows sent on a response
# this pipeline does not actually understand.
SUCCESS_STATUS = 204


def require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit("%s is required. See the Container Contract in "
                 "docs/architecture.md." % name)
    return value


def config_from_env():
    return {
        "db_path": require_env("SQLITE_DB_PATH"),
        "url": require_env("INFLUXDB_URL"),
        "token": require_env("INFLUXDB_TOKEN"),
        "org": require_env("INFLUXDB_ORG"),
        "bucket": require_env("INFLUXDB_BUCKET"),
        "batch_size": int(os.environ.get("FORWARDER_BATCH_SIZE",
                                         DEFAULT_BATCH_SIZE)),
        "interval": float(os.environ.get("FORWARDER_RETRY_INTERVAL_SECONDS",
                                         DEFAULT_RETRY_INTERVAL_SECONDS)),
    }


def write_url(url, org, bucket):
    """Build the InfluxDB OSS v2 write endpoint.

    precision=ns is sent explicitly even though it matches InfluxDB's default.
    The serializer emits nanoseconds, and a mismatch between what is emitted and
    what is declared shifts every point by orders of magnitude with no error
    from InfluxDB. Leaving it implicit is the one thing the spec rules out.
    """
    query = urllib.parse.urlencode(
        {"org": org, "bucket": bucket, "precision": "ns"})
    return "%s/api/v2/write?%s" % (url.rstrip("/"), query)


def write_points(url, org, bucket, token, body):
    """POST one batch of line protocol. Returns on success, raises on anything else.

    Every failure mode is the same failure mode here, deliberately. A network
    error, an auth rejection and a malformed point all raise, all leave the rows
    unsent, and all retry next cycle. This pipeline does not distinguish
    retryable from permanent failures, which means a permanently rejected write
    retries forever; the cycle log is what makes that visible.
    """
    request = urllib.request.Request(
        write_url(url, org, bucket),
        data=body.encode("utf-8"),
        method="POST",
    )
    request.add_header("Authorization", "Token %s" % token)
    request.add_header("Content-Type", "text/plain; charset=utf-8")

    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        if response.status != SUCCESS_STATUS:
            raise RuntimeError(
                "InfluxDB returned %d, expected %d" % (response.status, SUCCESS_STATUS))


def drain(conn, url, org, bucket, token, batch_size):
    """Push as much of the backlog as the link will take. Returns (pushed, failed).

    Batches are issued back to back for as long as they keep succeeding, so a
    link that has just come back drains a multi-day backlog as fast as InfluxDB
    accepts writes, rather than one batch per cycle. The first failure stops the
    drain: if the link just dropped again there is nothing to gain from pushing
    the next batch into it, and the rows are all still safely unsent.
    """
    pushed = 0

    while True:
        rows = outbox.fetch_unsent(conn, batch_size)
        if not rows:
            return pushed, 0

        try:
            write_points(url, org, bucket, token, lineprotocol.encode_batch(rows))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                RuntimeError, ValueError) as exc:
            LOG.warning("batch of %d failed, rows left unsent: %s", len(rows), exc)
            return pushed, len(rows)

        # Only now, on a confirmed 204.
        outbox.mark_sent(conn, [row["id"] for row in rows])
        pushed += len(rows)

        if len(rows) < batch_size:
            return pushed, 0


def run(db_path, url, org, bucket, token, batch_size, interval, stop_event):
    """Forward forever, or until stop_event is set.

    Opens its own SQLite connection, separate from the collector's.
    """
    conn = outbox.connect(db_path)
    LOG.info("started: target=%s org=%s bucket=%s batch=%d interval=%.1fs",
             url, org, bucket, batch_size, interval)

    try:
        while not stop_event.is_set():
            try:
                pushed, failed = drain(conn, url, org, bucket, token, batch_size)
                backlog = outbox.count_unsent(conn)
            except Exception as exc:
                # The outbox itself failed, e.g. the disk is full. Nothing to do
                # but say so and try again; the collector will be failing too.
                LOG.error("forward cycle failed: %s", exc)
                stop_event.wait(interval)
                continue

            # These three numbers are the whole diagnostic surface of this
            # pipeline. A backlog that never falls means a permanently rejected
            # write; a backlog that climbs means the link cannot keep up.
            log = LOG.warning if failed else LOG.info
            log("forward cycle: %d pushed, %d failed, %d unsent in backlog",
                pushed, failed, backlog)

            stop_event.wait(interval)
    finally:
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
