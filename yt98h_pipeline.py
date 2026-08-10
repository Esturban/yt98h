#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
# REUSE_CHECKED: yt98h_collector.py, yt98h_forwarder.py   Both loops already
# exist and each runs standalone. This adds only the process that hosts both and
# shuts them down together; it contains no pipeline logic of its own.
"""
Container entrypoint: run the collector and the forwarder together.

    uv run yt98h_pipeline.py

This is the process the Docker image starts. Two threads, two independent
loops, decoupled by the SQLite outbox: the collector never blocks on network
state and the forwarder never blocks a poll cycle. Neither knows the other
exists, they only share a file.

Both loops are validated before either starts, so a missing environment
variable fails immediately and visibly rather than after the collector has
been happily filling an outbox that no forwarder will ever drain.
"""

import logging
import signal
import sys
import threading

import yt98h_collector as collector
import yt98h_forwarder as forwarder

LOG = logging.getLogger("pipeline")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # Read both configurations up front. Either one missing a required variable
    # exits here, before a single reading is taken.
    collector_config = collector.config_from_env()
    forwarder_config = forwarder.config_from_env()

    stop_event = threading.Event()

    def request_stop(signum, _frame):
        LOG.info("signal %d received, stopping both loops", signum)
        stop_event.set()

    # docker stop sends SIGTERM. Handling it means both loops finish the cycle
    # they are in and close their connections, rather than being killed ten
    # seconds later mid write.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    threads = [
        threading.Thread(target=collector.run, name="collector",
                         kwargs=dict(stop_event=stop_event, **collector_config)),
        threading.Thread(target=forwarder.run, name="forwarder",
                         kwargs=dict(stop_event=stop_event, **forwarder_config)),
    ]

    LOG.info("starting collector and forwarder")
    for thread in threads:
        thread.start()

    # If either loop dies unexpectedly, stop the other and let the container's
    # restart policy bring the pair back up cleanly. A half-running pipeline is
    # worse than a restarting one: a collector with no forwarder fills the disk
    # silently, and a forwarder with no collector looks perfectly healthy while
    # no readings are being taken at all.
    while not stop_event.is_set():
        if not all(thread.is_alive() for thread in threads):
            dead = [t.name for t in threads if not t.is_alive()]
            LOG.error("loop exited unexpectedly: %s, stopping the pipeline", dead)
            stop_event.set()
            break
        stop_event.wait(1.0)

    for thread in threads:
        thread.join()

    LOG.info("pipeline stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
