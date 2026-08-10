# Edge pipeline

Reads the YT-98H every few seconds, writes every reading to a local SQLite
outbox, and pushes unsent rows to the command-center InfluxDB whenever the
optical link happens to be up. The outbox is what makes an outage of any length
cost delay instead of data.

Design and rationale: [`../docs/architecture.md`](../docs/architecture.md).

## Deploying a new poll

The same image runs on every poll. Rolling out poll 2 through poll 8 means
changing `SITE_ID` and pointing at the command center. Nothing is rebuilt per
site, nothing in this directory is edited per site.

```bash
git clone <this repo> && cd yt98h/edge

cat > .env <<'ENV'
INFLUXDB_URL=http://command-center:8086
INFLUXDB_TOKEN=<write-scoped token>
INFLUXDB_ORG=<org>
INFLUXDB_BUCKET=<bucket>
ENV

# The one line that differs per poll. Must be unique across the fleet.
sed -i 's/poll-01/poll-02/' docker-compose.yml

docker compose up -d
docker logs -f yt98h-pipeline
```

Three things to check on a new box before assuming it works:

1. `ls /dev/ttyUSB*` matches `SERIAL_PORT` and the `devices:` entry in
   `docker-compose.yml`. Change both together if the node differs.
2. The clock is right (A5 in the architecture doc). Timestamps are taken from
   this box's clock, and a wrong clock silently writes every point to the wrong
   time with no error anywhere.
3. `docker logs` shows both `poll ok: 5 readings written` and
   `forward cycle: N pushed, 0 failed, 0 unsent in backlog`.

## Environment variables

The full contract. Defaults are already set in `docker-compose.yml`, so in
practice only the first five need a decision per deployment.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SITE_ID` | Yes | `poll-01` | Identifies this poll. Must be unique across the fleet. Tagged onto every point. |
| `INFLUXDB_URL` | Yes | from `.env` | Command-center write endpoint |
| `INFLUXDB_TOKEN` | Yes | from `.env` | Write-scoped token |
| `INFLUXDB_ORG` | Yes | from `.env` | InfluxDB organization |
| `INFLUXDB_BUCKET` | Yes | from `.env` | Target bucket for `gas_reading` points |
| `SQLITE_DB_PATH` | Yes | `/data/outbox.sqlite3` | Outbox file, on a volume that survives reboots |
| `SERIAL_PORT` | No | `/dev/ttyUSB0` | Auto-detected when unset |
| `MODBUS_BAUD` | No | `9600` | Bench verified profile |
| `MODBUS_ADDRESSES` | No | `1,2,3,4,5` | The five gas channels |
| `POLL_INTERVAL_SECONDS` | No | `5` | Collector interval |
| `FORWARDER_RETRY_INTERVAL_SECONDS` | No | `15` | Forwarder interval, independent of the poll interval |
| `FORWARDER_BATCH_SIZE` | No | `500` | Rows per HTTP write, bounds any single request |

## Reading the logs

stdout is the only visibility this pipeline has, by design. The forwarder's
three numbers are the whole diagnostic surface:

| Log line | Means |
|---|---|
| `0 pushed, N failed, backlog growing` | Link down, or the token is wrong. Rows are safe, they retry forever. |
| `N pushed, 0 failed, 0 unsent` | Healthy. |
| backlog never reaches 0 while pushing | The link cannot keep up with 1 row/s. |
| `no response from [3]` | That channel is silent. Skipped, never written as a false zero. |

A backlog costs about 13 MB/day of disk. See the sizing table in the
architecture doc.

## The files

| File | What it does |
|---|---|
| `yt98h_pipeline.py` | Container entrypoint. Runs both loops as threads. |
| `yt98h_collector.py` | Polls every address, writes to the outbox. Imports the driver from `../yt98h_modbus.py`. |
| `yt98h_forwarder.py` | Drains unsent rows to InfluxDB in batches. |
| `yt98h_outbox.py` | SQLite schema and queue operations. |
| `yt98h_lineprotocol.py` | InfluxDB line protocol serialization. |
| `test_yt98h_pipeline.py` | Tests. No hardware and no InfluxDB needed. |

## Running without Docker

Each file runs standalone on a bench machine, PEP 723 style like the rest of
the repo:

```bash
uv run test_yt98h_pipeline.py     # 41 tests, no hardware needed
uv run yt98h_pipeline.py          # both loops, needs the env vars above
uv run yt98h_collector.py         # collector only
```
