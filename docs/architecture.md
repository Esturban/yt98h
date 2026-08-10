# Architecture: Edge-to-Command-Center Data Pipeline

**Status**: Draft, spec only. Nothing described here is implemented yet.
**Scope**: One "poll" (edge site). The same container image is redeployed
unchanged to future polls by changing environment variables.

## Overview

Each poll is a physical post with a Jetson Orin Nano connected to a command
center over an intermittent, unscheduled optical wireless link. The Jetson
reads a YT-98H gas transmitter every 5 to 10 seconds, writes every reading to
a local SQLite file immediately, and separately, best effort, forwards
unsent rows to a command-center InfluxDB over HTTP whenever the link happens
to be up. The hard requirement is zero data loss: a reading that reaches
SQLite must eventually reach InfluxDB, no matter how long the link stays
down.

This pipeline is analytics and logging only. The on-device alarm on the
transmitter itself is the real-time safety mechanism; a 5 to 10 second batch
delay here is an accepted tradeoff, not a safety gap.

**Out of scope, do not read this doc as covering it**: the optical link
itself (treated as an opaque, unmanaged external condition), any VPN, tunnel,
or relay that provides network reachability (someone else's infrastructure),
fleet orchestration or a device registry across the eventual 4 to 8+ polls,
and the separate AI PPE-compliance inference workload that happens to share
the same Jetson. That workload is a different concern on the same box and is
not referenced further in this document.

## Actors

| Actor | Role |
|---|---|
| YT-98H transmitter | Physical gas sensor, 5 channels, read over Modbus RTU/RS-485 |
| Collector | Process on the Jetson that polls the transmitter and writes raw readings to SQLite |
| SQLite outbox | Local durable file on the Jetson, the only thing standing between a reading and data loss |
| Forwarder | Process on the Jetson that pushes unsent SQLite rows to the command-center InfluxDB when reachable |
| Optical link | Physical wireless connection to the command center, intermittent and unmanaged by this pipeline |
| Command-center InfluxDB | Remote time-series store, receiving writes via its HTTP write API |
| Container image | Single Docker image bundling collector and forwarder, parameterized by env vars per poll |

## System Flow

```
YT-98H  --Modbus RTU-->  Collector  --write-->  SQLite outbox  --read unsent-->  Forwarder  --HTTP write-->  command-center InfluxDB
(addr 1-5)                (~5-10s poll)          (durable,           Forwarder     (best effort,           (measurement: gas_reading)
                                                   local disk)         loop         retries on next cycle)
```

Collector and forwarder are two independent loops in the same container.
They are decoupled by the SQLite outbox: the collector never blocks on
network state, and the forwarder never blocks the collector. This is what
makes an extended, unscheduled outage on the optical link harmless to data
collection.

### Step 1: Collect

**Actor**: Collector
**Action**: Every 5 to 10 seconds, read all 5 sensor addresses (extends the
existing `yt98h_modbus.py` block-read logic: full 32-register block per
address, never a single-register read). Decode `value` from `r31 / 10**r27`
per channel, per the register map already documented in the repo README.
**Output**: One decoded reading per channel, per poll cycle.

### Step 2: Persist locally (durable write)

**Actor**: Collector
**Action**: Write each reading to the SQLite outbox, one row per channel per
poll cycle, before anything touches the network.
**Why this order matters**: this is the step that satisfies zero data loss.
Once a row commits to SQLite, the reading survives a Jetson reboot, a power
loss, and any length of network outage. Nothing downstream of this step can
cause data loss, only delay.

### Step 3: Forward (best effort)

**Actor**: Forwarder
**Action**: On its own loop, independent of the collector's poll interval,
query SQLite for unsent rows and attempt an HTTP write to the
command-center InfluxDB write API.
**Batching**: the query is capped at `FORWARDER_BATCH_SIZE` rows (default 500),
and the forwarder keeps issuing back to back batches within a single cycle for
as long as batches keep succeeding, stopping when the backlog is empty or the
first batch fails. After a multi-day outage there may be hundreds of thousands
of unsent rows; pushing them as one HTTP request would mean a single multi-
megabyte POST that has to succeed or fail in its entirety over exactly the kind
of marginal link that caused the backlog. Line protocol imposes no hard row
limit per request, so this is a practical bound, not a protocol one: a few
hundred points per write keeps each request small enough to complete over a
weak link, and makes a failure cost one batch of progress rather than all of it.
**On success**: mark the pushed rows as sent in SQLite.
**On failure, of any kind**: do nothing to the rows, they remain unsent, and
retry on the next forwarder cycle. The forwarder treats "is the link up
right now" as opaque and unmanaged, it does not probe, does not alert on
down time, and does not change behavior based on how long the link has been
down.
**Overwrite semantics on retry**: because failure leaves rows untouched and
success is the only thing that advances state, a row is never marked sent
until InfluxDB has actually accepted it. InfluxDB identifies a point by its
measurement, tag set, and timestamp together, not by a separate row ID, so a
crash between "write succeeded" and "mark sent" does not create a duplicate
record on the next cycle. It overwrites the same point with identical field
values, a no-op. On InfluxDB OSS v2 (the target here, see A3), this overwrite
is deterministic: the existing and new field sets are unioned, with the new
write's fields winning on any conflict. If InfluxDB Cloud Serverless,
Dedicated, or Clustered is ever substituted for OSS v2 later, note that
same-timestamp same-tag-set write ordering is not guaranteed deterministic
there, a prior write may win instead. Since every retry in this pipeline
carries identical field values, that non-determinism is harmless regardless
of which write wins, but it is a real behavioral difference from OSS worth
knowing about if the target ever changes.

## Local Outbox Schema (SQLite)

One row per sensor reading, per poll cycle.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | Autoincrement, local only |
| `site_id` | TEXT | From container env, same on every row this container writes |
| `sensor_address` | INTEGER | 1 to 5, the Modbus address |
| `gas_type` | TEXT | Decoded from r25, e.g. H2S, CO, O2, LEL, CO2 |
| `value` | REAL | Decoded reading, `r31 / 10**r27` |
| `raw_register_value` | INTEGER | Raw r31, kept for traceability back to the source register |
| `reading_timestamp_utc` | TEXT (ISO 8601) | Set at the moment of the read in Step 1, this is the value forwarded to InfluxDB as the point timestamp |
| `sent` | INTEGER (0/1) | Default 0, set to 1 only after a confirmed InfluxDB write |
| `created_at_utc` | TEXT (ISO 8601) | When the row was written to SQLite, for local debugging only, never forwarded |

The forwarder's query is simply "all rows where `sent = 0`, oldest first."

```sql
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

-- The forwarder's only query is "sent = 0, oldest first", and it runs every
-- cycle for the life of the device. This index keeps it from degrading into a
-- full table scan once a long outage has left tens of thousands of rows behind.
-- Leading on `sent` serves the filter, trailing `id` serves the ordering, so
-- the whole query is answered from the index.
CREATE INDEX IF NOT EXISTS idx_readings_unsent ON readings (sent, id);
```

## InfluxDB Schema (command-center)

**Measurement**: `gas_reading`

**Tags** (low cardinality, indexed, used for filtering once multiple polls
share one database):

| Tag | Example |
|---|---|
| `site_id` | `poll-01` |
| `sensor_address` | `3` |
| `gas_type` | `O2` |

Line protocol tag values are always unquoted strings, there is no separate
integer tag type. `sensor_address` is stored as `INTEGER` in the SQLite
outbox (see Local Outbox Schema above) but must be serialized as a string
tag here, e.g. `sensor_address=3`, not a numeric type.

**Fields**:

| Field | Type | Notes |
|---|---|---|
| `value` | float | Decoded reading in the sensor's native unit |
| `raw_register_value` | integer | Raw r31, kept for traceability |

Line protocol requires an explicit `i` suffix on integer field values, e.g.
`raw_register_value=42i`. A bare number like `42` is parsed as a float. The
forwarder must append this suffix when serializing `raw_register_value`, or
it silently writes as a float and violates this schema.

**Timestamp**: the actual reading time (`reading_timestamp_utc` from the
outbox row), not the time of ingest. This is deliberate: after an extended
outage, a batch of old readings arrives late, and each point must land at
the time it was actually measured, not the time it happened to be pushed.

## Failure Modes and Recovery

| Failure | What happens | Recovery |
|---|---|---|
| Jetson reboot or power loss mid-write | SQLite commits are atomic; a reading is either fully in the outbox or not written at all. Anything the collector had not yet committed is simply lost as if the poll cycle never ran (accepted: the next 5-10s poll produces a fresh reading). Nothing already in SQLite is affected. | Collector and forwarder restart with the container (see restart policy in the container contract). Forwarder resumes from wherever `sent = 0` rows exist, no special recovery logic needed. |
| Network down for an extended period | Forwarder's writes fail every cycle. Rows accumulate in SQLite with `sent = 0`. Collector is unaffected and keeps writing. | No action needed. Forwarder keeps retrying every cycle. When the optical link returns, the backlog drains in batches of `FORWARDER_BATCH_SIZE` rows per HTTP write, oldest first, not in one request. The forwarder keeps issuing batches within a cycle while they succeed, so a returning link drains as fast as InfluxDB accepts writes without ever building a single oversized POST. Disk space for SQLite growth during a long outage is a capacity assumption, see Assumptions. |
| InfluxDB write API rejects a batch (e.g. malformed point, auth failure, server error) | Rejected rows are not marked sent. | Retried on the next forwarder cycle exactly like a network failure. A rejection that is permanent (e.g. bad credentials) will retry forever and never succeed; this pipeline does not distinguish retryable from permanent failures. Operator visibility into a stuck backlog is not designed here (see Assumptions/Open Questions). |
| Partial batch failure (some points in a push accepted, some rejected) | Only rows confirmed accepted are marked sent. Any row not confirmed stays `sent = 0` and is retried. | Same retry path as a full failure. A retried row that was actually already accepted overwrites the same point (same measurement, tag set, and timestamp) with identical field values in InfluxDB, a no-op, not a duplicate record (accepted tradeoff, see overwrite semantics note in Step 3). |
| Two distinct sensor readings for the same `site_id` + `sensor_address` + `gas_type` land on the exact same `reading_timestamp_utc` value | Because InfluxDB identifies a point by measurement, tag set, and timestamp together, the second write silently overwrites the first. This is real data loss, not a retry artifact, and InfluxDB returns no error. | Not recoverable after the fact. Prevented structurally, not by luck: the collector must capture `reading_timestamp_utc` at a precision fine enough that two distinct poll cycles for the same channel, 5 to 10 seconds apart, cannot land on the same value. Nanosecond precision (matching InfluxDB's native timestamp resolution) is recommended, millisecond is the practical minimum. This must be an explicit property of the collector's implementation, not an assumption that "5 to 10 seconds apart" is inherently safe. |
| Clock drift on the Jetson | Reading timestamps are taken from the Jetson's own clock at read time. If that clock is wrong, every point written during the drift window lands at the wrong time in InfluxDB, silently. | Not handled by this pipeline. Flagged as an assumption below, not solved here. |

## Container Contract

One Docker image, bundling collector and forwarder as the two processes it
runs. Redeploying to a new poll means running this same image with a
different environment, nothing rebuilt, nothing changed in code.

| Env var | Required | Purpose |
|---|---|---|
| `SITE_ID` | Yes | Identifies this poll. Written into every SQLite row and every InfluxDB tag. Must be unique per poll across the fleet. |
| `SERIAL_PORT` | No | Modbus serial device path, e.g. `/dev/ttyUSB0`. Left unset, the collector auto-detects as `yt98h_modbus.py` already does. |
| `MODBUS_BAUD` | No | Defaults to `9600`, matches the transmitter's confirmed profile. |
| `MODBUS_ADDRESSES` | No | Defaults to `1,2,3,4,5`, the five sensor channels. |
| `POLL_INTERVAL_SECONDS` | No | Collector poll interval. Defaults within the accepted 5 to 10 second range. |
| `SQLITE_DB_PATH` | Yes | Path to the outbox file, expected to be a mounted volume that survives container restarts and Jetson reboots. |
| `INFLUXDB_URL` | Yes | Command-center InfluxDB write endpoint. |
| `INFLUXDB_TOKEN` | Yes | Write-scoped auth token for the InfluxDB write API. |
| `INFLUXDB_ORG` | Yes | InfluxDB organization the bucket lives in (v2-style write API, see Assumptions). |
| `INFLUXDB_BUCKET` | Yes | Target bucket for `gas_reading` points. |
| `FORWARDER_RETRY_INTERVAL_SECONDS` | No | How often the forwarder attempts to drain unsent rows. Independent of `POLL_INTERVAL_SECONDS`. Defaults to `15`. |
| `FORWARDER_BATCH_SIZE` | No | Maximum rows pushed in a single HTTP write. Defaults to `500`. Bounds the size of any one request so that draining a large backlog does not turn into one enormous POST. |

`INFLUXDB_URL`/`INFLUXDB_TOKEN`/`INFLUXDB_ORG`/`INFLUXDB_BUCKET` are named as
a single set deliberately: a second command center later means a second set
of these four values (a second forwarder target), not a redesign of the
contract. This is not built now, just kept from being awkward later.

The `/api/v2/write` endpoint (InfluxDB OSS v2) requires `org` and `bucket`
as query parameters, both already covered by `INFLUXDB_ORG`/`INFLUXDB_BUCKET`
above, plus an `Authorization: Token <token>` header carrying
`INFLUXDB_TOKEN`. It also accepts an optional `precision` query parameter
that tells InfluxDB how to interpret the timestamp on each point, defaulting
to nanoseconds if omitted. This is a real correctness risk worth naming
explicitly: whatever unit the forwarder actually converts
`reading_timestamp_utc` to before the HTTP write, seconds, milliseconds, or
nanoseconds, must match the `precision` parameter sent on that same request.
A mismatch silently corrupts every timestamp by orders of magnitude with no
error from InfluxDB. The forwarder should either always convert to
nanoseconds (matching the default) or always pass an explicit, consistent
`precision` value, either is acceptable, leaving it implicit is not.

### Logging

There is no dashboard on the Jetson and none is planned, so stdout is the only
visibility this pipeline has, and it is therefore not optional. Both loops log
one line per cycle to stdout, which Docker captures and `docker logs` replays:
the collector logs each poll cycle's outcome, either success with the number of
readings written, or failure with the error; the forwarder logs each cycle's
outcome, rows pushed, rows failed, and the current unsent backlog size. Those
three forwarder numbers are what make the two failure modes that are otherwise
completely silent, a permanently rejected write (bad credentials) and a backlog
that is growing faster than it drains, diagnosable by hand from `docker logs`
alone. Logs go to stdout only, never to a file: writing logs to the same disk
that holds the outbox would put log growth in competition with the backlog for
the capacity budgeted in A4. Nothing else is designed here, no log levels, no
rotation, no structured format.

## Deferred

Security hardening of this pipeline itself, auth on the InfluxDB write
beyond the token, transport encryption between the Jetson and the command
center, and credential storage for `INFLUXDB_TOKEN` on the Jetson, is not
designed in this document. It is cataloged here so it is not lost, and needs
a dedicated pass before this goes to production.

## Assumptions

| # | Assumption | Risk if wrong |
|---|---|---|
| A1 | `SQLITE_DB_PATH` is mounted on storage that survives a Jetson reboot (not container-ephemeral, not tmpfs). | A reboot during a network outage would silently drop the entire unsent backlog, violating the zero data loss requirement. |
| A2 | The container's restart policy brings the collector and forwarder back up automatically after a Jetson reboot or crash, with no manual intervention. | A reboot could leave both processes down indefinitely with no one aware. |
| A3 | InfluxDB at the command center runs OSS v2 (self-hosted, TSM engine), using the org/bucket/token HTTP write API described above. This schema and API shape have now been verified as an accurate description of the InfluxDB OSS v2 write API contract specifically, it is not an unverified guess. What remains an assumption is which InfluxDB product or deployment the command center actually runs. | If it's actually InfluxDB v1 or a materially different auth model, the forwarder's write calls and the env vars above need to change. If it's InfluxDB Cloud Serverless, Dedicated, or Clustered instead of OSS v2 (the newer IOx-based products), the write API shape is largely compatible, but the deterministic overwrite behavior this design relies on for retry idempotency (see Step 3) does not hold there, same-timestamp same-tag-set write ordering is not guaranteed deterministic on those products. |
| A4 | Local disk on the Jetson has at least **1 GB free** for the outbox. That covers a **72 hour** outage more than twenty times over, at the sizing below. | A sufficiently long outage could fill the disk, causing the collector's SQLite writes to start failing, which is a real data loss path this spec does not currently cover. At 1 GB the disk is not the binding constraint on any outage anyone expects to actually happen. |
| A5 | The Jetson's system clock is kept reasonably accurate (e.g. NTP, even if only synced during the brief windows the link is up). | Silent timestamp corruption on every reading during a drift window, not caught by any check in this pipeline. |

### Outbox sizing (basis for A4)

This is a sizing estimate, not a guarantee. It is deliberately pessimistic at
every step, and the conclusion is that disk is not a real constraint here.

| Input | Value | Basis |
|---|---|---|
| Rows per poll cycle | 5 | One per sensor address, the five channels |
| Poll interval, worst case | 5 s | Fast end of the accepted 5 to 10 s range |
| Sustained row rate | ~1 row/s | 5 rows / 5 s, the worst case of the two |
| Bytes per row | ~150 B | Conservative. Two ISO 8601 timestamp strings at 32 chars each dominate; the rest is short text and small ints plus a float. Includes SQLite record and b-tree overhead and the `(sent, id)` index entry. |

From those: **86,400 rows/day**, about **13 MB/day** of outbox growth while the
link is down. Nothing is ever deleted by this pipeline, so this is growth, not
steady state.

**Stated worst-case outage: 72 hours.** The reasoning is that the optical link
is unscheduled, unmanaged, and generates no alert when it drops, so the recovery
time is bounded not by the fault but by how long until a person is next at the
site or happens to notice the data stopped. A long weekend is the realistic
outer edge of that, and it is the number to size against. A 72 hour backlog is
**259,200 rows, about 39 MB**.

That is small enough that the honest conclusion is to stop optimizing: even a
full **30 day** outage is only about **389 MB**, and the 1 GB free space
assumed in A4 covers roughly **11 weeks** of continuous downtime. The disk fills
long after every other part of this arrangement has failed for other reasons.

## Open Questions

- How is a stuck backlog (e.g. permanently rejected writes due to bad
  credentials) surfaced to an operator? Partially answered: the forwarder logs
  rows pushed, rows failed, and backlog size every cycle to stdout (see
  Logging), which makes it diagnosable by hand via `docker logs`. What is still
  not designed is anything that pushes that fact to an operator who is not
  already looking, no local dashboard exists on the Jetson by design.
- ~~What is the disk capacity budget for the SQLite outbox?~~ Answered above,
  see Outbox sizing. What remains open is what should happen if it is somehow
  exceeded anyway: today the collector's writes would simply start failing, and
  nothing in this pipeline detects or degrades gracefully in that case.
