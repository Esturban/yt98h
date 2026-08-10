# /// script
# requires-python = ">=3.9"
# dependencies = ["pymodbus", "pyserial"]
# ///
"""
Modbus RTU sliding-window register sweep & logger for YT-98H gas registers.

USAGE AND SETUP
---------------------------------------------------
First run script:
  1. Creates a throwaway virtual environment at
         %USERPROFILE%\\Documents\\.venv
  2. Installs pymodbus into it
  3. Re-launches itself using that venv's Python to actually run the sweep.
  4. When the sweep finishes (you press Ctrl+C, or it errors out), it
     deletes that .venv folder again, leaving no trace on the host PC.

Just run it with whatever Python is already on the machine:
    python modbus_register_sweep.py

Edit the CONFIG section below first to match your RS-485 adapter/device.

While it's running: let it run a couple of full passes at rest, then
trigger your gas test, let it run a couple more passes, then press
Ctrl+C to stop (this also triggers venv cleanup).

Afterwards: copy/pull sweep_log_<timestamp>.csv off the remote PC and
open in Excel. Each column is one absolute register address (e.g.
"REG_22"); scan for a column whose value changes at the row/timestamp
matching your test.
"""

import csv
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

VENV_ENV_FLAG = "MODBUS_SWEEP_IN_VENV"  # set by the bootstrap when re-launching

# ----------------------- CONFIG: edit these ------------------------------
SERIAL_PORT   = ""          # "" = auto-detect. "COM5" Win, "/dev/ttyUSB0" Linux
BAUDRATE      = 9600
PARITY        = "N"         # "N", "E", or "O"
STOPBITS      = 1
BYTESIZE      = 8
SLAVE_IDS     = [1, 2, 3, 4, 5]   # one Modbus address per gas channel

FUNCTION_CODE = 3           # confirmed working: FC03 (Read Holding Registers)
QUANTITY      = 32          # bench-confirmed block size (start=0 count=32)
                             # you've verified a different value also works

SWEEP_START   = 0           # first address to probe
SWEEP_END     = 32          # last address to probe (exclusive-ish; last
                             # window may extend slightly past this)
STEP          = 32          # how far to move the window each request;
                             # = QUANTITY for non-overlapping coverage,
                             # smaller (e.g. 4) for finer/overlapping
                             # resolution at the cost of more requests

REQUEST_DELAY_SEC = 0.15    # small pause between requests in a pass, so
                             # you don't hammer the bus back-to-back
SWEEP_PAUSE_SEC   = 2.0     # pause between full sweep passes

OUTPUT_DIR = "."            # where to write the CSV (remote-friendly: use
                             # a shared/mapped path if you need it pulled
                             # automatically)
# ---------------------------------------------------------------------------


def read_window(client, address, count, slave):
    """Read one window of registers. Returns list of ints, or None + error text."""
    try:
        if FUNCTION_CODE == 3:
            result = client.read_holding_registers(
                address=address, count=count, device_id=slave
            )
        else:
            result = client.read_input_registers(
                address=address, count=count, device_id=slave
            )
        if result.isError():
            return None, str(result)
        return result.registers, None
    except Exception as exc:
        return None, str(exc)


def build_windows():
    """Build the list of starting addresses for the sliding window sweep."""
    starts = []
    addr = SWEEP_START
    while addr < SWEEP_END:
        starts.append(addr)
        addr += STEP
    return starts


def main():
    # Imported here (not at module top) so the outer bootstrap/launcher
    # process - which runs BEFORE pymodbus is installed - never needs it.
    from pymodbus.client import ModbusSerialClient
    from serial.tools import list_ports

    port = SERIAL_PORT
    if not port:
        cands = [p.device for p in list_ports.comports()
                 if not (sys.platform == "darwin" and "/tty." in p.device)]
        for hint in ("usbserial", "ttyUSB", "wchusb", "SLAB", "COM"):
            match = [c for c in cands if hint.lower() in c.lower()]
            if match:
                port = match[0]
                break
        if not port:
            print("No serial port found. Set SERIAL_PORT manually.")
            return

    client = ModbusSerialClient(
        port=port,
        baudrate=BAUDRATE,
        parity=PARITY,
        stopbits=STOPBITS,
        bytesize=BYTESIZE,
        timeout=1,
    )

    if not client.connect():
        print(f"Could not open {port}. Check the port name and that "
              f"nothing else (like Modbus Poll) is using it.")
        return

    window_starts = build_windows()
    max_addr = window_starts[-1] + QUANTITY - 1
    all_addrs = list(range(SWEEP_START, max_addr + 1))
    headers = ["timestamp", "pass_number", "slave_id", "window_start", "status"] + \
              [f"REG_{a}" for a in all_addrs]

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(OUTPUT_DIR, f"sweep_log_{run_stamp}.csv")

    print(f"Connected on {port}. Logging to {log_path}")
    print(f"Sweeping registers {SWEEP_START}-{max_addr} in windows of "
          f"{QUANTITY} (step {STEP}), FC{FUNCTION_CODE:02d}, "
          f"slaves {SLAVE_IDS}.")
    print("Let it run a couple of passes at rest, trigger your gas test, "
          "let it run a couple more, then press Ctrl+C to stop.\n")

    with open(log_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        pass_number = 0
        try:
            while True:
                pass_number += 1
                # one row per window read, so a partial/failed window
                # doesn't blank out the whole pass
                for slave in SLAVE_IDS:
                  for start in window_starts:
                    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    values, err = read_window(client, start, QUANTITY, slave)

                    row_values = {}
                    if values is not None:
                        for offset, val in enumerate(values):
                            row_values[start + offset] = val
                        status = "OK"
                    else:
                        status = f"ERROR: {err}"

                    row = [ts, pass_number, slave, start, status]
                    row += [row_values.get(a, "") for a in all_addrs]
                    writer.writerow(row)
                    f.flush()

                    print(ts, f"pass {pass_number}", f"slave {slave}",
                          f"reg {start}:",
                          values if values is not None else status)

                    time.sleep(REQUEST_DELAY_SEC)

                time.sleep(SWEEP_PAUSE_SEC)

        except KeyboardInterrupt:
            print("\nStopped. Log saved to", log_path)

    client.close()


def bootstrap():
    """
    Runs in the SYSTEM Python (no pymodbus required). Creates a throwaway
    venv under Documents\\.venv, installs pymodbus into it, re-launches
    this same script inside that venv to do the actual work, then deletes
    the venv once the worker process exits (whether it finished normally,
    errored, or was stopped with Ctrl+C).
    """
    documents_dir = os.path.join(os.path.expanduser("~"), "Documents")
    venv_dir = os.path.join(documents_dir, ".venv")

    os.makedirs(documents_dir, exist_ok=True)

    venv_python = os.path.join(
        venv_dir, "Scripts", "python.exe"
    ) if os.name == "nt" else os.path.join(venv_dir, "bin", "python")

    try:
        if not os.path.exists(venv_python):
            print(f"Creating temporary virtual environment at {venv_dir} ...")
            subprocess.run(
                [sys.executable, "-m", "venv", venv_dir], check=True
            )

        print("Installing pymodbus into the temporary environment ...")
        subprocess.run(
            [venv_python, "-m", "pip", "install", "--quiet",
             "--upgrade", "pip"],
            check=True,
        )
        subprocess.run(
            [venv_python, "-m", "pip", "install", "--quiet", "pymodbus"],
            check=True,
        )

        print("Starting the sweep ...\n")
        env = os.environ.copy()
        env[VENV_ENV_FLAG] = "1"

        try:
            subprocess.run([venv_python, os.path.abspath(__file__)], env=env)
        except KeyboardInterrupt:
            # Ctrl+C reached this parent process too (shared console) -
            # the worker's own handler already saved/closed the CSV.
            pass

    finally:
        print(f"\nRemoving temporary virtual environment at {venv_dir} ...")
        # Give the child's python.exe a moment to fully release file
        # handles before deleting the folder (mainly matters on Windows).
        for attempt in range(5):
            try:
                if os.path.exists(venv_dir):
                    shutil.rmtree(venv_dir)
                break
            except OSError:
                time.sleep(1.0)
        else:
            print(f"Could not fully remove {venv_dir} - "
                  f"you may need to delete it manually.")

        print("Cleanup complete.")


if __name__ == "__main__":
    # Already inside a venv that has pymodbus? Just run. No bootstrap needed.
    try:
        import pymodbus  # noqa: F401
        _READY = True
    except ImportError:
        _READY = False

    if _READY or os.environ.get(VENV_ENV_FLAG) == "1":
        # We're already running inside the throwaway venv - do the real work.
        main()
    else:
        # Outer/system Python - set up the venv, run the worker in it, clean up.
        bootstrap()
