# Edge pipeline image: collector and forwarder, per docs/architecture.md.
# One image, redeployed to every poll unchanged, parameterized only by the
# environment variables in the Container Contract.
#
# DELIBERATE DEVIATION from this repo's uv / PEP 723 convention. Every script
# here still carries its inline dependency block and `uv run yt98h_pipeline.py`
# still works on a bench machine, which is what that convention is for. Inside
# the container the dependency is installed at build time instead, because this
# image runs on a Jetson behind an intermittent optical link: resolving anything
# at container start would mean the pipeline cannot restart while the link is
# down, which is precisely the condition it exists to survive. Build time
# install turns a network dependency at the worst possible moment into a layer.

FROM python:3.12-slim

# The single dependency, from yt98h_modbus.py's PEP 723 block. Pinned so a
# rebuild months from now produces the same image.
RUN pip install --no-cache-dir pyserial==3.5

WORKDIR /app

# The build context is the repo ROOT, not edge/, because the collector reuses
# the bench verified driver at the root rather than vendoring a copy. Build with
# `docker compose build` from this directory, which sets that up; a bare
# `docker build .` from edge/ will fail on the first COPY, by design.
#
# Copied by name rather than `COPY . .`: the bench tooling, the vendored
# ModScan32 binaries and the docs have no business in an edge image. Everything
# lands flat in /app, so the repo root that yt98h_collector.py adds to sys.path
# is simply never needed here.
COPY yt98h_modbus.py ./
COPY edge/yt98h_outbox.py \
     edge/yt98h_lineprotocol.py \
     edge/yt98h_collector.py \
     edge/yt98h_forwarder.py \
     edge/yt98h_pipeline.py \
     ./

# stdout is the only visibility this pipeline has by design, so it must not sit
# in a buffer waiting for a page to fill while an operator reads docker logs.
ENV PYTHONUNBUFFERED=1

# Runs as root. The container needs read and write on the RS-485 device node,
# and hardening is explicitly deferred in docs/architecture.md.
CMD ["python", "yt98h_pipeline.py"]
