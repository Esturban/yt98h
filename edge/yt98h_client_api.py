#!/usr/bin/env python3
# REUSE_CHECKED: yt98h_lineprotocol.py   Same shape as the line protocol
# module: a pure payload builder plus a small stdlib HTTP call, no SQLite
# knowledge, so it can be unit tested without a live outbox or a live server.
# No existing client for the client's ingest API exists anywhere in this repo
# or the wider repos tree, this is new.
"""
Client API: serialize outbox rows and POST them to the client's live
production ingest API (IR4 PPE Compliance dashboard), a second delivery
target alongside the command-center InfluxDB write in yt98h_forwarder.py.
See docs/architecture.md for how this fits the pipeline and the "sent"
semantics decision.

Environment (read by yt98h_forwarder.py, not by this module):

    IR4_INGEST_URL      optional, defaults to DEFAULT_INGEST_URL below
    IR4_DEVICE_TOKEN    required only when this path is enabled, no default

UNCONFIRMED PAYLOAD SHAPE: the field names, the flat (non-nested) structure,
and the absence of a device_id field are inferred from the existing
third-party agent's own local debug log line, not from a successful request
against this endpoint. A live probe confirmed the URL, the X-Device-Token
auth header, and the error envelope shape, but not the body schema, no valid
device token was available to get past auth. build_payload() below is
deliberately the one place this assumption lives, so correcting it once a
real token is available is a one-function change, not a refactor.

GAS_FIELD is the register/gas map already documented in the repo README,
mapping the outbox's gas_type column to the client API's field name.
"""

import json
import logging
import urllib.request

LOG = logging.getLogger("client_api")

DEFAULT_INGEST_URL = "https://ir4.ispc-ai.com/api/ingest/gas-readings"

GAS_FIELD = {
    "H2S": "h2s_ppm",
    "CO": "co_ppm",
    "O2": "o2_pct",
    "LEL": "lel_pct",
    "CO2": "co2_ppm",
}

# Matches HTTP_TIMEOUT_SECONDS in yt98h_forwarder.py: a write that has not
# completed in half a minute over this link is a failed cycle either way.
HTTP_TIMEOUT_SECONDS = 30


def build_payload(rows):
    """Flatten one poll cycle's outbox rows into the client API's JSON body.

    A row whose gas_type is not in GAS_FIELD is skipped rather than raising:
    that only happens if the register map ever grows a channel this endpoint
    does not know about, and dropping the field is the safer failure than
    rejecting the whole cycle. A channel missing from `rows` (silent this
    cycle, per the collector's "skip, don't fabricate a zero" convention) is
    likewise simply absent from the payload, never sent as 0.0.
    """
    payload = {}
    for row in rows:
        field = GAS_FIELD.get(row["gas_type"])
        if field is None:
            continue
        payload[field] = float(row["value"])
    return payload


def group_by_cycle(rows):
    """Split a batch of outbox rows into one list per poll cycle.

    created_at_utc is set once per poll cycle by the collector (see
    poll_cycle() in yt98h_collector.py) and shared across every channel read
    that cycle, so it is the natural grouping key here: the client API wants
    one flattened object per full cycle, not one POST per channel like the
    InfluxDB path. Insertion order is preserved so a group's row ids can be
    marked sent independently of any other group in the same batch.
    """
    groups = {}
    order = []
    for row in rows:
        key = row["created_at_utc"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return [groups[key] for key in order]


def post_readings(url, token, payload):
    """POST one payload. Returns on any 2xx, raises on anything else.

    Mirrors write_points() in yt98h_forwarder.py deliberately: a network
    error, an auth rejection, and a non-2xx status all raise the same way, so
    the caller can treat this exactly like the InfluxDB write, leave the rows
    unsent, and retry next cycle with no special casing.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("X-Device-Token", token)
    request.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError("client API returned %d" % response.status)
