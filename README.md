# YT-98H Gas Transmitter

Reads 5 gas sensors over Modbus RTU (RS-485).
Works on Windows, Linux and macOS.

## Start here

```bash
# Step 0
chmod +x ./dev_path.sh
chmod 666 [/dev/USB0 OR WHATEVER DEVICE IT IS] # use this to map the device (so it will change when connected directly)

uv run yt98h_modbus.py detect     # find the device
uv run yt98h_modbus.py map        # see every register and every gas value
uv run yt98h_modbus.py watch      # live values, updates every second
```

`uv run` installs everything by itself. No Python setup needed.

To make the CSV file for the vendor, run this and press Ctrl+C after a few
seconds:

```bash
uv run modbus_register_sweep.py
```

It writes `sweep_log_<date>_<time>.csv`.

## How to talk to the device

```
speed      9600 8N1
function   FC03  (read holding registers)
addresses  1, 2, 3, 4, 5
read       start = 0, count = 32
```

**Always read all 32 registers in one request.**
If you read one register alone, this device gives back the wrong data.

## The 5 sensors

Each sensor has its own Modbus address.

| Address | Gas | Unit | Full range | REG_31 raw | Real value |
|---|---|---|---|---|---|
| 1 | H2S | ppm | 200 | 0 | 0 ppm |
| 2 | CO | ppm | 2000 | 0 | 0 ppm |
| 3 | O2 | %VOL | 30 | 209 | 20.9 %VOL |
| 4 | LEL | %LEL | 100 | 0 | 0 %LEL |
| 5 | CO2 | ppm | 50000 | 834 | 834 ppm |

Zero is correct in clean air. Only O2 and CO2 show a value in a normal room.

## The 32 registers

One full read. Same data you get from `uv run yt98h_modbus.py map`.

| Reg | Addr 1 | Addr 2 | Addr 3 | Addr 4 | Addr 5 | What it is |
|---|---|---|---|---|---|---|
| r0 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r1 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r2 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r3 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r4 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r5 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r6 | 201 | 201 | 201 | 201 | 201 | same on all sensors |
| r7 | 201 | 201 | 201 | 201 | 201 | same on all sensors |
| r8 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r9 | 10135 | 10135 | 10135 | 10135 | 10135 | air pressure, 1013.5 hPa |
| r10 | 201 | 201 | 201 | 201 | 201 | same on all sensors |
| r11 | 180 | 180 | 180 | 180 | 180 | same on all sensors |
| r12 | 1 | 1 | 1 | 1 | 1 | same on all sensors |
| r13 | 1 | 1 | 1 | 1 | 1 | same on all sensors |
| r14 | 1 | 1 | 1 | 1 | 1 | same on all sensors |
| r15 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r16 | 31 | 31 | 31 | 31 | 31 | same on all sensors |
| r17 | 1 | 1 | 1 | 1 | 1 | same on all sensors |
| r18 | 1 | 1 | 1 | 1 | 1 | same on all sensors |
| r19 | 3 | 3 | 3 | 3 | 3 | same on all sensors |
| r20 | 64113 | 64113 | 64113 | 64113 | 64113 | same on all sensors |
| r21 | 5 | 5 | 5 | 5 | 5 | how many sensors, 5 |
| r22 | 1688 | 1688 | 1688 | 1688 | 1688 | device ID |
| r23 | 59727 | 59727 | 59727 | 59727 | 59727 | device ID |
| r24 | 12802 | 12802 | 12802 | 12802 | 12802 | device ID |
| r25 | 2 | 1 | 3 | 65 | 18 | **which gas** |
| r26 | 0 | 0 | 2 | 3 | 0 | **unit** |
| r27 | 1 | 0 | 1 | 0 | 0 | **decimal places** |
| r28 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r29 | 2000 | 2000 | 300 | 100 | 50000 | **full range** |
| r30 | 0 | 0 | 0 | 0 | 0 | same on all sensors |
| r31 | 0 | 0 | 209 | 0 | 834 | **THE GAS VALUE** |

**r0 to r24 are the same on every sensor.** They are device settings.
**r25 to r31 change per sensor.** Those are the ones you want.

## How to get the gas value

```
value = REG_31 / 10 ** REG_27
```

| Register | Meaning |
|---|---|
| REG_25 | which gas: 1=CO, 2=H2S, 3=O2, 18=CO2, 65=LEL |
| REG_26 | unit: 0=ppm, 1=mg/m3, 2=%VOL, 3=%LEL |
| REG_27 | decimal places |
| REG_29 | full range |
| REG_31 | **the gas value** |

Example, address 3:
REG_31 = 209, REG_27 = 1, so 209 / 10 = 20.9.
REG_26 = 2, which means %VOL.
Answer: **20.9 %VOL oxygen**.

Example, address 5:
REG_31 = 834, REG_27 = 0, so no change.
REG_26 = 0, which means ppm.
Answer: **834 ppm CO2**.

## Change settings

Open `yt98h_modbus.py`. The top has a block marked `CONFIG. EDIT THIS PART.`
Most people never need to touch it.

| Setting | What it is |
|---|---|
| `PORT` | Leave `""` and it finds the port by itself. Or write `"COM5"`. |
| `BAUD` | Speed. `9600` for this device. |
| `ADDRESSES` | The sensor list: `[1, 2, 3, 4, 5]` |
| `GAS_TYPES` | Which number means which gas |

If a sensor shows as `code 7` instead of a name, add one line to `GAS_TYPES`:
`7: "NH3",`

If the script picks the wrong port, pass it on the command line instead:

| Your computer | Add this |
|---|---|
| Windows | `--port COM5` |
| Linux | `--port /dev/ttyUSB0` |
| macOS | `--port /dev/cu.usbserial-XXXXXXXX` |

## The files

| File | What it does |
|---|---|
| `yt98h_modbus.py` | Main tool. detect, map, watch. |
| `modbus_register_sweep.py` | Makes the CSV for the vendor. |
| `Get-ModbusDiagnostics-1.ps1` | Windows only. Checks your PC, not the sensor. |

## If it does not work

1. **Swap the A and B wires.** This is the answer most of the time.
2. Check 24V power is on and the screen is lit.
3. Wait 2 to 5 minutes. Sensors need to warm up.
4. Set Output Mode to `RS485` in the screen menu.
5. Close other programs using the port (Modbus Poll, PuTTY).
6. Run `uv run yt98h_modbus.py detect`. It tries every speed and address.

Two common mistakes:

- pymodbus 3.14 uses `device_id=`, not `slave=`. Old code crashes.
- On macOS use `/dev/cu.*`, never `/dev/tty.*`. It will freeze.

CO2 updates slowly, every 15 to 45 seconds. Wait a full minute before you
think it is broken.
