#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
# Consolidates several earlier one-off scripts into a single tool. The vendor's
# own modbus_register_sweep.py is kept alongside this, because they want its CSV.
"""
YT-98H multi gas transmitter, Modbus RTU driver and register mapper.

Runs on Windows, Linux and macOS.

EASIEST WAY TO RUN IT. No venv, no install, nothing to set up:

    uv run yt98h_modbus.py detect

The dependency is declared inline at the top of this file (PEP 723), so uv
fetches pyserial into a throwaway environment automatically. Works identically
on Windows, Linux and macOS.

Or the classic way:

    pip install pyserial
    python yt98h_modbus.py detect

COMMANDS
--------
    detect      find the port, baud, parity, addresses
    map         dump the full register block per channel
    watch       live gas readings, one line per second
    raw --addr 1 --start 0 --count 32

Add --port COM5 (Windows), --port /dev/ttyUSB0 (Linux) or
--port /dev/cu.usbserial-XXXX (macOS) to skip auto detection.

CONFIRMED HARDWARE PROFILE (bench verified 2026-08-09)
------------------------------------------------------
    serial      9600 8N1
    function    FC03 Read Holding Registers. FC04 is NOT supported.
    addresses   1, 2, 3, 4, 5. One Modbus address per gas channel.
    access      ALWAYS read start=0 count=32 in a single request.

TWO FIRMWARE QUIRKS. Both of these make stock Modbus libraries report a dead
bus even when the wiring is perfect. This driver handles both.

  1. The byte count field carries a REGISTER count, not a byte count.
     A count=1 read returns:  01 03 02 00 00 00 00 72 33
     The field says 02, but four data bytes follow, and the CRC covers all of
     them. A spec compliant parser reads 7 bytes, fails CRC, and reports
     "no response". We accept a payload of 2 x count_field bytes.

  2. Single register reads return ALIASED data. Asking for register 35 gives
     back what the count=32 block holds at index 19. Never read single
     registers from this device. Always read the whole block.

REGISTER LAYOUT, start 0 count 32
---------------------------------
    r0  to r24   identical on all five addresses. Device global config.
                 r9  = 10135, likely barometric pressure 1013.5 hPa
                 r21 = 5, the channel count
                 r22, r23, r24 = serial / ID words
    r25          gas type code: 1=CO, 2=H2S, 3=O2, 18=CO2, 65=LEL
    r26          unit code. 0=ppm, 1=mg/m3, 2=%VOL, 3=%LEL
    r27          decimal places
    r29          full scale range
    r31          LIVE GAS READING

    Scaled value = r31 / 10 ** r27

THE FIVE CHANNELS
-----------------
    addr 1   H2S   ppm    full scale 200.0     0.0 in clean air
    addr 2   CO    ppm    full scale 2000      0 in clean air
    addr 3   O2    %VOL   full scale 30.0      20.9, exact ambient
    addr 4   LEL   %LEL   full scale 100       0 in clean air
    addr 5   CO2   ppm    full scale 50000     ~830 to 930, normal indoors

The CO2 channel updates only every 15 to 45 seconds because the NDIR sensor is
heavily averaged. Watch it for a full minute before deciding it is frozen.
No sample pump is needed for any of these readings.
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Install it with:  pip install pyserial")

# ==========================================================================
#  CONFIG. EDIT THIS PART. Nothing else in this file needs to change.
# ==========================================================================

# Your serial port.
# Leave it as "" and the script finds it by itself.
# If that picks the wrong one, write it here.
#   Windows:  "COM5"
#   Linux:    "/dev/ttyUSB0"
#   macOS:    "/dev/cu.usbserial-XXXXXXXX"
PORT = ""

# Serial speed. The YT-98H uses 9600 8N1.
# Only change these if your device is set differently.
BAUD = 9600          # 9600, 4800, 19200, 38400, 2400, 57600, 115200
PARITY = "N"         # "N" = none, "E" = even, "O" = odd
STOPBITS = 1         # 1 or 2

# One Modbus address per gas sensor.
# Add or remove numbers here if your unit has a different number of sensors.
ADDRESSES = [1, 2, 3, 4, 5]

# Which gas is on which sensor.
# The number on the left is the value the device reports in REG_25.
# The name on the right is what you want printed on screen.
# If a reading shows up as "code 7", add a line here: 7: "NH3",
GAS_TYPES = {
    1: "CO",
    2: "H2S",
    3: "O2",
    18: "CO2",
    65: "LEL",
}

# What the unit number in REG_26 means.
UNITS = {
    0: "ppm",
    1: "mg/m3",
    2: "%VOL",
    3: "%LEL",
}

# ==========================================================================
#  ADVANCED. You should not need to touch anything below here.
# ==========================================================================

# How the device is read. Always one block of 32 registers, starting at 0.
# Reading a single register gives back WRONG data on this device.
BLOCK_START = 0
BLOCK_COUNT = 32

# Which register holds what, inside that block of 32.
R_GAS_TYPE = 25      # which gas this sensor measures
R_UNIT = 26          # ppm, %VOL, %LEL
R_DECIMALS = 27      # how many decimal places
R_RANGE = 29         # biggest value the sensor can read
R_VALUE = 31         # THE GAS READING

# Used by "detect" when the settings above do not work.
SCAN_BAUDS = [9600, 4800, 19200, 38400, 2400, 57600, 115200]
SCAN_PARITIES = ["N", "E", "O"]


# --------------------------------------------------------------------------
# Modbus RTU framing
# --------------------------------------------------------------------------

def crc16(data: bytes) -> int:
    """Standard Modbus RTU CRC-16, polynomial 0xA001."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc_ok(frame: bytes) -> bool:
    """True if the last two bytes are a valid CRC over everything before them."""
    if len(frame) < 4:
        return False
    calc = crc16(frame[:-2])
    return frame[-2:] == bytes([calc & 0xFF, (calc >> 8) & 0xFF])


def build_request(addr: int, fc: int, start: int, count: int) -> bytes:
    body = bytes([addr, fc, start >> 8, start & 0xFF, count >> 8, count & 0xFF])
    crc = crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def parse_response(raw: bytes, addr: int, fc: int):
    """Decode a response. Returns (kind, payload).

    kind is one of:
        "ok"        payload is the list of register values
        "exception" payload is the Modbus exception code
        "silence"   nothing came back
        "noise"     bytes came back but no valid CRC frame was found
    """
    if not raw:
        return "silence", None

    # Try a few starting offsets. RS-485 direction turnaround sometimes
    # prepends a junk byte, and half duplex adapters may echo our request.
    for off in range(min(12, len(raw))):
        chunk = raw[off:]
        if len(chunk) < 5 or chunk[0] != addr:
            continue

        # Exception frame: addr, fc|0x80, code, crc_lo, crc_hi
        if chunk[1] == (fc | 0x80) and crc_ok(chunk[:5]):
            return "exception", chunk[2]

        if chunk[1] != fc:
            continue

        count_field = chunk[2]
        # Try the spec reading (field is a byte count) and then this device's
        # quirk (field is a register count, so the payload is twice as long).
        for datalen in (count_field, 2 * count_field):
            need = 3 + datalen + 2
            if datalen and len(chunk) >= need and crc_ok(chunk[:need]):
                data = chunk[3:3 + datalen]
                return "ok", [(data[i] << 8) | data[i + 1]
                              for i in range(0, len(data) - 1, 2)]

    return "noise", raw


# --------------------------------------------------------------------------
# Serial transport
# --------------------------------------------------------------------------

def open_port(port, baud=BAUD, parity=PARITY, stopbits=STOPBITS):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.parity = parity
    ser.stopbits = stopbits
    ser.bytesize = 8
    ser.timeout = 0.3
    ser.write_timeout = 2
    ser.open()
    time.sleep(0.05)
    ser.reset_input_buffer()
    return ser


def transact(ser, addr, fc, start, count, baud=BAUD):
    """Send one request, read whatever comes back, decode it."""
    req = build_request(addr, fc, start, count)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(req)
    ser.flush()

    char_time = 11.0 / baud
    ser.timeout = max(0.08, 40 * char_time + 0.05)
    buf = ser.read(1)
    if buf:
        # Keep reading while bytes keep arriving close together.
        ser.timeout = max(0.015, 4 * char_time)
        while len(buf) < 512:
            more = ser.read(128)
            if not more:
                break
            buf += more
    return parse_response(buf, addr, fc)


def autodetect_port(explicit=None):
    """Pick a USB serial port. Works on Windows, Linux and macOS.

    On macOS use the /dev/cu.* node, never /dev/tty.*: opening a tty node
    blocks waiting for carrier detect, which looks exactly like a hung script.
    """
    if explicit:
        return explicit
    if PORT:
        return PORT

    candidates = []
    for p in list_ports.comports():
        name = p.device
        if sys.platform == "darwin" and "/tty." in name:
            continue
        candidates.append(name)

    if not candidates:
        sys.exit("No serial ports found. Plug in the USB-RS485 adapter.\n"
                 "  Windows: check Device Manager for a COM port\n"
                 "  Linux:   ls /dev/ttyUSB*   "
                 "(you may need: usermod -aG dialout $USER)\n"
                 "  macOS:   ls /dev/cu.*")

    # Prefer something that looks like a USB serial bridge.
    for hint in ("usbserial", "ttyUSB", "wchusb", "SLAB", "COM"):
        for c in candidates:
            if hint.lower() in c.lower():
                return c
    return candidates[0]


# --------------------------------------------------------------------------
# Channel decoding
# --------------------------------------------------------------------------

def read_channel(ser, addr, baud=BAUD):
    """Read one channel's 32 register block and decode it.
    Returns a dict, or None if the channel did not answer."""
    kind, payload = transact(ser, addr, 3, BLOCK_START, BLOCK_COUNT, baud)
    if kind != "ok" or len(payload) < BLOCK_COUNT:
        return None

    regs = payload
    decimals = regs[R_DECIMALS]
    raw = regs[R_VALUE]
    return {
        "address": addr,
        "raw_value": raw,
        "value": raw / (10 ** decimals) if decimals else float(raw),
        "decimals": decimals,
        "unit": UNITS.get(regs[R_UNIT], f"code{regs[R_UNIT]}"),
        "unit_code": regs[R_UNIT],
        "range": regs[R_RANGE],
        "gas_type_code": regs[R_GAS_TYPE],
        "gas": GAS_TYPES.get(regs[R_GAS_TYPE], f"code{regs[R_GAS_TYPE]}"),
        "registers": regs,
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_detect(args):
    """Find the port, then the baud/parity/addresses that actually answer."""
    port = autodetect_port(args.port)
    print(f"Port: {port}")
    print("Trying the bench confirmed profile first: 9600 8N1, FC03, addr 1-5\n")

    combos = [(BAUD, PARITY)] + [(b, p) for b in SCAN_BAUDS
                                 for p in SCAN_PARITIES
                                 if (b, p) != (BAUD, PARITY)]
    for baud, parity in combos:
        try:
            ser = open_port(port, baud, parity)
        except Exception as exc:
            sys.exit(f"Cannot open {port}: {exc}")
        found = []
        try:
            for addr in range(1, 33):
                kind, _ = transact(ser, addr, 3, BLOCK_START, BLOCK_COUNT, baud)
                if kind in ("ok", "exception"):
                    found.append(addr)
        finally:
            ser.close()

        if found:
            print(f"FOUND at {baud} 8{parity}{STOPBITS}")
            print(f"  addresses that answer: {found}")
            print("  function code        : FC03")
            print("  block to read        : start=0 count=32")
            print(f"\nNext:  python {sys.argv[0]} map --port {port} "
                  f"--baud {baud} --parity {parity}")
            return 0
        print(f"  {baud} 8{parity}{STOPBITS}: nothing")

    print("\nNothing answered on any setting. Ranked causes:")
    print("  1. A and B are swapped. By far the most common fault.")
    print("  2. Transmitter unpowered, or still in its 2 to 5 minute warm up.")
    print("  3. The adapter is a plain TTL/RS-232 UART, not an RS-485 converter.")
    print("  4. Wrong port selected. Pass --port explicitly.")
    return 4


def cmd_map(args):
    """Dump the full register block for every channel, decoded."""
    port = autodetect_port(args.port)
    ser = open_port(port, args.baud, args.parity)
    print(f"{port}  {args.baud} 8{args.parity}{STOPBITS}  FC03 start=0 count=32\n")
    channels = []
    try:
        for addr in args.addrs:
            ch = read_channel(ser, addr, args.baud)
            if ch is None:
                print(f"addr {addr}: no response")
                continue
            channels.append(ch)
            print(f"addr {addr}:  {ch['gas']:<4} {ch['value']:>8g} {ch['unit']:<5}"
                  f"   range 0-{ch['range']}"
                  f"   raw r31={ch['raw_value']} decimals={ch['decimals']}"
                  f"   code {ch['gas_type_code']}")
    finally:
        ser.close()

    if not channels:
        return 4

    print("\nFull register block per address:\n")
    header = "reg  " + "".join(f"addr{c['address']:<8}" for c in channels)
    print(header)
    print("-" * len(header))
    for i in range(BLOCK_COUNT):
        row = f"r{i:<3} " + "".join(f"{c['registers'][i]:<12}" for c in channels)
        same = len({c["registers"][i] for c in channels}) == 1
        print(row + ("" if same else "   <== differs per channel"))

    print("\nRegisters marked 'differs per channel' are the per sensor fields:")
    print(f"  r{R_GAS_TYPE} gas type   r{R_UNIT} unit   r{R_DECIMALS} decimals"
          f"   r{R_RANGE} full scale   r{R_VALUE} LIVE VALUE")
    return 0


def cmd_watch(args):
    """Live readings, one line per poll."""
    port = autodetect_port(args.port)
    ser = open_port(port, args.baud, args.parity)
    print(f"{port}  {args.baud} 8{args.parity}{STOPBITS}  addresses {args.addrs}")
    print("Ctrl+C to stop. CO2 updates only every 15 to 45 s.\n")
    try:
        while True:
            cells = []
            for addr in args.addrs:
                ch = read_channel(ser, addr, args.baud)
                cells.append(f"a{addr}=--" if ch is None
                             else f"{ch['gas']}={ch['value']:g}{ch['unit']}")
            print(f"{time.strftime('%H:%M:%S')}  " + "   ".join(cells))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ser.close()
    return 0


def cmd_raw(args):
    """Send one arbitrary read and print the decoded registers."""
    port = autodetect_port(args.port)
    ser = open_port(port, args.baud, args.parity)
    try:
        kind, payload = transact(ser, args.addr, args.fc, args.start,
                                 args.count, args.baud)
        print(f"kind={kind}")
        if kind == "ok":
            for i, v in enumerate(payload):
                print(f"  r{args.start + i}: {v}")
        elif kind == "noise":
            print(f"  bytes: {payload.hex(' ')}")
        elif kind == "exception":
            print(f"  Modbus exception code {payload}")
    finally:
        ser.close()
    return 0


def main():
    p = argparse.ArgumentParser(
        description="YT-98H Modbus RTU driver and register mapper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'detect' first if you do not know the port or settings.")
    p.add_argument("--port", help="COM5 / /dev/ttyUSB0 / /dev/cu.usbserial-XXXX")
    p.add_argument("--baud", type=int, default=BAUD)
    p.add_argument("--parity", default=PARITY, choices=["N", "E", "O"])
    p.add_argument("--addrs", type=int, nargs="+", default=ADDRESSES)

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect", help="find port, baud, parity and addresses")
    sub.add_parser("map", help="dump the decoded register block per channel")

    w = sub.add_parser("watch", help="live gas readings")
    w.add_argument("--interval", type=float, default=1.0)

    r = sub.add_parser("raw", help="one arbitrary read")
    r.add_argument("--addr", type=int, default=1)
    r.add_argument("--fc", type=int, default=3)
    r.add_argument("--start", type=int, default=0)
    r.add_argument("--count", type=int, default=32)

    args = p.parse_args()
    return {"detect": cmd_detect, "map": cmd_map,
            "watch": cmd_watch, "raw": cmd_raw}[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
