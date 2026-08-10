#!/usr/bin/env python3
# REUSE_CHECKED: none   Searched the repos tree for an existing InfluxDB client
# or line protocol serializer to reuse; nothing. Deliberately not pulling in
# influxdb-client either: this is one string format, roughly thirty lines, and a
# client library would add a dependency and its transport to an edge container
# whose whole job is to survive without either. Format rules are the ones
# verified in docs/architecture.md, "InfluxDB Schema (command-center)".
"""
InfluxDB v2 line protocol serialization for gas_reading points.

Stdlib only, no dependencies, so the test suite can exercise it without pyserial
and without a live InfluxDB.

One point looks like this:

    gas_reading,gas_type=O2,sensor_address=3,site_id=poll-01 value=20.9,raw_register_value=209i 1786104000000000000
    |__________| |_______________________________________| |_________________________________| |_________________|
     measurement                tag set                                field set                  timestamp (ns)

Three rules in here are silent-corruption traps rather than syntax errors, which
is why each has a test of its own:

  1. An integer field needs an explicit `i` suffix. Without it InfluxDB parses
     the value as a float and the bucket's field type quietly stops matching the
     documented schema. No error is returned.
  2. Tags are always unquoted strings. `sensor_address` is an INTEGER column in
     the outbox but there is no integer tag type, so it is serialized bare.
  3. Timestamps are nanoseconds, matching InfluxDB's default precision and the
     `precision=ns` parameter the forwarder sends. A unit mismatch between these
     two shifts every point by orders of magnitude, again with no error.
"""

from datetime import datetime, timezone

MEASUREMENT = "gas_reading"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def escape_tag(value):
    """Escape a tag key or tag value for line protocol.

    Commas, equals signs and spaces are the three characters that would
    otherwise end the tag set early and turn the point into a parse error. In
    practice only SITE_ID can realistically contain one, since it is operator
    supplied, but a malformed point is rejected by InfluxDB and a rejected point
    in this pipeline means a backlog that never drains, so it is worth the three
    replacements.
    """
    return (str(value)
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace("=", "\\=")
            .replace(" ", "\\ "))


def iso_to_unix_nanos(text):
    """Convert an ISO 8601 outbox timestamp to integer nanoseconds since epoch.

    Integer arithmetic throughout, deliberately. Going via datetime.timestamp()
    would route the value through a float, and float64 cannot hold nanosecond
    resolution at a 2020s epoch offset, so the low digits would be quietly
    rounded away. Those low digits are what keep two poll cycles from colliding
    on the same point.

    A timestamp with no offset is treated as UTC. The collector always writes an
    explicit +00:00, so this only matters for hand-written data.
    """
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = parsed - _EPOCH
    seconds = delta.days * 86400 + delta.seconds
    return seconds * 1_000_000_000 + delta.microseconds * 1000


def encode_point(row):
    """Serialize one outbox row (dict or sqlite3.Row) as a single point."""
    tags = {
        "site_id": row["site_id"],
        "sensor_address": row["sensor_address"],
        "gas_type": row["gas_type"],
    }
    # Sorted tag keys are InfluxDB's documented write-performance
    # recommendation, and they make the output byte-stable across runs.
    tag_set = ",".join("%s=%s" % (escape_tag(k), escape_tag(v))
                       for k, v in sorted(tags.items()))

    # repr() on a float never drops the decimal point, so a whole-number reading
    # such as 834.0 stays a float field rather than collapsing to "834" and
    # landing in the bucket as an integer.
    field_set = "value=%r,raw_register_value=%di" % (
        float(row["value"]), int(row["raw_register_value"]))

    nanos = iso_to_unix_nanos(row["reading_timestamp_utc"])
    return "%s,%s %s %d" % (MEASUREMENT, tag_set, field_set, nanos)


def encode_batch(rows):
    """Serialize a batch of outbox rows into one request body.

    Points are newline separated with no trailing newline. An empty batch
    returns an empty string, which the forwarder treats as nothing to send
    rather than posting an empty body.
    """
    return "\n".join(encode_point(r) for r in rows)
