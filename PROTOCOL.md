# Creality CFS — RS485 protocol notes

Interoperability notes for talking to a Creality CFS over its RS485 bus. Compiled
from observing live traffic and from analyzing Creality's `mcu_util_485` for the
purpose of writing an independent, compatible flasher. Bus/command layout was
cross-checked against the archworks.co K2-Plus reverse-engineering write-up.

## Physical layer

- Half-duplex **RS485**, **230400** baud, **8N1**.
- On K1-series printers the CFS hangs off a **CH340 USB-serial adapter** (USB VID
  `1a86`), i.e. `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` → `ttyUSB0`.
  Some printers have a **second** `1a86` adapter (`usb-1a86_USB_Single_Serial_…`)
  that is **not** the CFS bus, so match the full `USB_Serial-if00` name rather than
  just the vendor id.
- Several CFS boxes share the same bus in series. Stock Klipper gives them the
  addresses `0x01`…`0x04` at startup.

## Addresses and device types

From Creality's own address manager on the printer (`auto_addr_wrapper.py`, the
module behind the `auto_addr` section). The same bus scheme can carry three kinds
of devices; on K1 printers only the boxes are enabled:

| device type (byte after the cmd echo) | device | addresses | type broadcast |
|---|---|---|---|
| `01` | CFS box | `0x01`…`0x04` | `0xFE` |
| `02` | closed-loop motor | `0x81`…`0x84` | `0xFD` |
| `03` | belt-tension motor | `0x91`, `0x92` | `0xFC` |

`0xFF` addresses every device on the bus. (The K2 shares one bus between its CFS and
its closed-loop motors, which is why these ranges exist.)

Addresses stick to the boxes across reboots: stock Klipper saves each address's
12-byte UUID at the end of `printer.cfg`

```
#*# [auto_addr]
#*# mb_addr_table_uniids = ...
```

and at startup gives every saved UUID its address again with `0xA0` to `0xFE`, without
an enumerate. Only boxes it does not know yet are found with `0xA1`. So "box 2" stays
the same physical box until the bus changes.

## Frame format

```
[0xF7] [addr] [length] [status] [cmd] [data ...] [crc8]
```

- `0xF7` — sync/head byte.
- `addr` — device address (`0x01`…`0x04` = CFS boxes after enumeration; `0xFE` =
  broadcast; `0xEB` = enter-bootloader broadcast).
- `length` — `len(data) + 3` (counts status + cmd + crc).
- `status` — `0xFF` for **app-mode** requests, `0x00` for **bootloader-mode** requests.
- `cmd` — command byte.
- `crc8` — CRC-8, polynomial `0x07`, computed over `[length, status, cmd, data...]`
  (i.e. it **excludes** the `0xF7` head byte and the `addr` byte).

CRC-8 reference implementation:

```python
def crc8(data, poly=0x07):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc
```

Sanity check: the "enter bootloader" broadcast `F7 EB 03 FF 56 CF` has
`crc8([0x03,0xFF,0x56]) == 0xCF`.

## Two command sets

A CFS runs either its **application** or its **bootloader**, and they speak
different command sets.

### App mode (status `0xFF`)

Selected commands (address `0x01`):

| cmd  | meaning                    |
|------|----------------------------|
| 0x14 | `GET_VERSION_SN` → version + serial (ASCII; leading digits are the version) |
| 0x15 | `GET_HARDWARE_STATUS`      |
| 0x55 | `COMMUNICATION_TEST` → ACK |
| 0x56 | enter bootloader. As the `0xEB` broadcast below it hits **every** box; sent to one box address (`F7 02 03 FF 56 CF` for box 2) only that box enters the loader and ACKs `00 56` |
| 0xA2 | identity/online → device type (`01` = CFS) + mode (`00` app / `01` loader) + 12-byte UUID. A box in the loader that has an address answers it too, with mode `01` |

Box commands used by the stock K1 firmware (names from the `Box_cmd` table in
Creality's `box_wrapper`; payloads as seen on the bus, see "Filament change"):

| cmd  | Creality name | request data | reply data |
|------|---------------|--------------|------------|
| 0x02 | `GET_RFID` | slot mask | ASCII, e.g. `B:unknown;`, `B:busy;` |
| 0x03 | `GET_REMAIN_LEN` | slot mask | 4 bytes, `ff ff ff ff` = unknown |
| 0x04 | `SET_BOX_MODE` | `00 01` = no slot active · `<slot> 00` = this slot is printing | — |
| 0x05 | `GET_BUFFER_STATE` | — | 1 byte, `00` = middle |
| 0x08 | `GET_FILAMENT_SENSOR_STATE` | `00` = sensors at the slot inlets | slot mask of slots with filament (`0f` = all four) |
| 0x0A | `GET_BOX_STATE` | — | 6 bytes: temperature °C, humidity %, … (see "Values") |
| 0x0D | `SET_PRE_LOADING` | slot mask + `01` = on | — |
| 0x0E | `MEASURING_WHEEL` | `01` = read | 4-byte float, big-endian |
| 0x0F | `TIGHTEN_UP_ENABLE` | `00` | — |
| 0x10 | `EXTRUDE_PROCESS` (load) | slot mask + stage + `00` | stage 0: 1 byte · stage 5: measuring-wheel float · others: — |
| 0x11 | `RETRUDE_PROCESS` (unload) | slot mask + stage | — |

Slots are a bit mask: A = `01`, B = `02`, C = `04`, D = `08`.

### Bootloader mode (status `0x00`)

Enter the bootloader with the broadcast frame:

```
F7 EB 03 FF 56 CF
```

Then enumerate and address the device(s):

| cmd  | addr | data                     | meaning                         |
|------|------|--------------------------|---------------------------------|
| 0xA1 | 0xFE | `FE FE`                  | enumerate (devices announce, incl. 12-byte UUID) |
| 0xA0 | 0xFE | `[new_addr] [UUID(12)]`  | assign an address to a device by UUID (CFS → `0x01`) |

These address commands are the same in app and bootloader mode and all use status
`0x00`. Creality's names (`auto_addr_wrapper.py`):

| cmd  | Creality name | sent to | reply data |
|------|---------------|---------|------------|
| 0xA1 | `CMD_GET_SLAVE_INFO` | type broadcast (`0xFE`), data `FE FE` | `[type] [mode] [uuid]`, only from boxes **without** an address |
| 0xA0 | `CMD_SET_SLAVE_ADDR` | type broadcast, data `[addr] [uuid]` | `[type] [mode] [uuid]` from the new address |
| 0xA2 | `CMD_ONLINE_CHECK` | one address | `[type] [mode] [uuid]` |
| 0xA3 | `CMD_GET_ADDR_TABLE` | one address | `[type] [mode] [uuid]` |
| 0x0B | `CMD_LOADER_TO_APP` | `0xFF`, data `01` | leave the bootloader and start the app (next section) |

`[mode]` is `00` app / `01` bootloader in every reply.

### Leaving the bootloader without a power cycle

Stock Klipper does this itself. Its address manager runs a loop (every ~1 s, every
10 s while printing). Whenever a reply has marked a box as being in the bootloader
(`addr 0x02 in loader mode` in `klippy.log`), the loop's "loader check" sends

```
F7 FF 04 00 0B 01 C8        (CMD_LOADER_TO_APP to 0xFF, data 01)
```

once per round, until the box answers from its app again. This is the same frame
grant0013 uses on the K2.

- Measured on K1 Max: a box that only **entered** the bootloader (nothing flashed)
  is back in its app a few seconds after stock Klipper starts.
- Sent by hand with two boxes on the bus (box 2 in the bootloader, box 1 in its
  app, Klipper stopped): no reply frame came back (only one stray `00` byte). Three
  seconds later box 2 answered the enumerate from its **app**, without an address
  (`00 a1 01 00 <uuid>`), and box 1 was still in its app at `0x01`. So only the box
  in the bootloader reacts.
- Sent **addressed** (`F7 02 04 00 0B 01 …`) instead of to `0xFF`, the box gave no
  reply and stayed in the bootloader. Use the broadcast.
- Why the app-mode box ignores it: in the CFS app (1.5.3) `0x0B` expects **two** data
  bytes, so the one-byte frame is rejected. The error reply the code would build was
  not seen on the bus.
- `0xF0 [02]` (bootloader "startup") did **not** leave the bootloader on an unflashed
  box (ACK `00 F0 75`, box stayed).
- **Not tested yet: after a real flash.** It cannot start an app that the bootloader
  did not accept (that box stays in the bootloader whatever you send). Whether it
  starts a freshly and correctly flashed app is open; so far every good flash was
  followed by a power cycle.

Once the CFS is at `0x01`, all flash operations use **`cmd = 0xF0`** with a
sub-command in the first data byte:

| data           | meaning                                         |
|----------------|-------------------------------------------------|
| `00`           | get version (ASCII)                             |
| `03`           | get sector size (1 byte)                         |
| `06`           | erase application flash                          |
| `01`           | request firmware update                          |
| `<len 4B LE>`  | firmware length (whole-file byte count, little-endian) |
| `<chunk ...>`  | firmware data chunk (no sub-command byte; raw)   |
| `02`           | startup / boot the application                   |

Each request expects a response (used as an ACK); retry up to 3×.

## Flash sequence

1. Broadcast **enter bootloader** (`F7 EB 03 FF 56 CF`).
2. **Enumerate** (`0xA1` → `0xFE`), read the device UUID from the response.
3. **Assign** address `0x01` (`0xA0` → `0xFE`, data `01` + UUID).
4. `0xF0 [00]` — get version (verify which firmware is currently installed).
5. `0xF0 [03]` — get sector size. Then compute the transfer chunk size as
   `chunk = (sector_byte * 0xFC) & 0xFF`.
6. `0xF0 [06]` — erase the application region (bootloader is preserved).
7. `0xF0 [01]` — request update.
8. `0xF0 [len]` — send the firmware length as 4 little-endian bytes
   (the **entire** file size; the whole `.bin` is sent, from offset 0).
9. `0xF0 [chunk]` — send the firmware in chunks (each ≤ 252 data bytes) until EOF.
10. `0xF0 [02]` — startup; the CFS boots the new application.

After a power-cycle, read the version again in app mode (`0x14`) to confirm.

What to expect after step 10:

- `startup` ACKs, but the box **stays in the bootloader**. A power cycle starts the
  new app; that is the verified way. Whether `CMD_LOADER_TO_APP` (see "Leaving the
  bootloader without a power cycle") also does it after a real flash is not tested
  yet.
- Every step above ACKs with the same reply (`F7 01 04 00 F0 75 00`), on good and on
  bad flashes alike. **An ACK is not proof the app was accepted.** The proof is
  `0x14` in app mode after the power cycle.
- `0xF0 [00]` returns `<boot_ver>-<app_ver>`. When the application region holds no
  valid app, the `app_ver` part is all zero bytes, i.e. the version reads
  `cfs0_050_G32-` with nothing after the dash. Seen on a box whose flash was not
  accepted (next section). Re-entering the bootloader straight after a **good**
  flash gave a different answer: an empty reply with status `0x01`. Both cleared
  up only after a power cycle (good flash) or a re-flash (bad one).

## Filament change (stock K1 sequence)

Reconstructed from 116 real colour changes in a K1 Max's `klippy.log`
(2026-10-28 to 2026-11-06, box 2 at `0x02`, CFS firmware 1.5.2/1.5.3). The stock
`box_wrapper` logs every frame and its own step names, so no extra capture was
needed. Note when reading such a log: a command line is written only once its
reply has arrived, so each reply appears **just above** its command.

Commands below are written without framing: `ff 10 08 05 00` = status `ff`,
cmd `0x10`, data `08 05 00`.

### At Klipper start (each box)

| request | meaning | reply |
|---|---|---|
| `ff 14` | version + serial | ASCII, e.g. `1531000…` |
| `ff 0d 0f 01` | pre-loading on, all four slots | OK |
| `ff 04 00 01` | box mode: no slot active | OK |
| `ff 08 00` | filament sensors at the slot inlets | `0f` |

Then `ff 0a` (box state) roughly every 2 s per box.

### One colour change, slot C → slot D (about 70 s)

1. **Cut** (printer only): the head moves to the cutter until the cut sensor
   triggers. No box traffic.
2. **Unload slot C** (`04`):

   | request | what happens | time |
   |---|---|---|
   | `ff 04 00 01` | box mode: no slot active | immediate |
   | `ff 11 04 00` | prepare unload | ~1 s |
   | *(printer)* | extruder retracts 20 mm | |
   | `ff 11 04 01` | box winds the filament back into the slot; the reply comes when done | ~30 s |

   Afterwards the toolhead filament sensor reads empty.
3. **Load slot D** (`08`):

   | request | what happens | reply |
   |---|---|---|
   | `ff 08 00` | check the slot sensors | `0f` |
   | `ff 10 08 00 00` | stage 0: feed forward; the reply comes when done | `00`, after ~10 s |
   | `ff 10 08 04 00` | stage 4 | OK |
   | `ff 10 08 05 00` | stage 5: polled every 2 s while the box pushes towards the head | measuring-wheel float |
   | `ff 10 08 06 00` | stage 6: stop, sent once the printer detects filament at the head | OK |
   | `ff 05` | buffer state | `00` = middle |
   | `ff 04 08 00` | box mode: slot D is printing | OK |

   Creality's own log calls the stop "stage7"; on the bus it is stage `06`. Its
   "stage8" steps after that are printer-side only.
4. **Purge** (printer only): the volume comes from the slicer's flush matrix
   × 0.7 (in the example 196 mm³ = 57 mm at 250 °C).

While printing, the printer reads the measuring wheel (`ff 0e 01`) about every
90 s. All 116 changes in the log follow this pattern for slots A–D; only the slot
mask differs.

### Values

- **Measuring wheel** (`0x0E`, and `0x10` stage 5): a 4-byte big-endian float.
  `c5 e9 9b 73` = −7475.43; Creality's log prints the same number
  (`[get_measuring_wheel] data_hex: 0xc5e99b73, data:-7475.43`). The unit is not
  confirmed: during 90 s of printing the magnitude rose by 185, which fits mm.
- **Box state** (`0x0A`), 6 bytes, e.g. `17 33 00 00 00 00` / `19 33 00 02 04 00`:
  - byte 4 is what Creality's log prints as `mode` (`2` while pre-loading),
  - byte 1 = temperature in °C, byte 2 = relative humidity in %: on an idle box
    they moved between 22–24 and 50–52, matching the values on the CFS box's own
    display,
  - the other bytes are not decoded yet.

## Several boxes on one bus

Measured on a K1 Max with two G32 boxes in series (CH340 RS485, Klipper stopped,
2026-11-06). Box UUIDs `54 62 30 02 18 53 33 11 52 35 38 32` and
`88 18 30 02 a4 34 b7 07 39 38 33 33`.

### What works

- **Enumerate does not collide.** With both boxes unaddressed, one `0xA1` to `0xFE`
  returned two separate, CRC-valid frames, one after the other:
  ```
  00 a1 01 00 88 18 30 02 a4 34 b7 07 39 38 33 33
  00 a1 01 00 54 62 30 02 18 53 33 11 52 35 38 32
  ```
  So a host can read every box's UUID in one go, as long as it keeps reading for
  the whole reply window instead of taking the first frame.
- **Addressed enter-bootloader.** `0x56` sent to one box address (`F7 02 03 FF 56 CF`)
  is ACKed with `00 56` and moves **only** that box into the loader. The other box
  kept answering `0xA2` with mode `00` the whole time.
- **Re-addressing in the loader by UUID.** A box that just entered the loader has
  lost its address. `0xA0` to `0xFE` with `[addr] [UUID]` gives it back without any
  `0xA1` first, because the UUID is already known from the app-mode `0xA2`. Reply:
  `00 a0 01 01 <uuid>` (type `01`, mode `01` = loader). `0xF0 [00]` and `0xF0 [03]`
  then work at that address.

### What collides

- **Broadcast `0xA2` to `0xFE`** with two unaddressed boxes: both answer at once and
  the host reads one garbled frame with a bad CRC. With addressed boxes it gets no
  answer at all. Query `0xA2` per address, never as a broadcast.

### What does NOT work: flashing through the addressed path

Box 2 entered the loader via addressed `0x56`, was re-addressed by `0xA0` (no
`0xA1`), and was then flashed with the normal `0xF0` sequence. **Every step ACKed**
(`erase`, `request`, `len`, all 1373 data frames, `startup`, each `00 f0 75 00`). Yet:

- the loader afterwards reported `cfs0_050_G32-` with an empty app part,
- the box stayed in the loader, **also after a full power cycle**,
- re-flashing a known-good image (1.5.2) the same way gave the same result.

The **same `.bin` files** flashed fine through the single-box path (`0xEB`
broadcast → `0xA1` → `0xA0` → flash → power cycle), with only that box on the bus.
Both boxes now run 1.5.3 that way.

Working guess, **not verified**: `0xA1` in the loader arms the write session, and
without it the loader ACKs the frames but does not commit them. Until that is
tested, flash one box at a time with only that box connected. (`cfstool` refuses
`--box` together with `--flash` for this reason.)

### Getting a box out of the loader

| situation | what brought the box back to its app |
|---|---|
| loader entered, **nothing flashed** | `F7 FF 04 00 0B 01 C8` (`CMD_LOADER_TO_APP` to `0xFF`), sent by stock Klipper's loader check or by hand: back in its app within 3 s, without an address. The other box, in its app on the same bus, was not affected |
| after a **good** flash | a **power cycle** (verified). `CMD_LOADER_TO_APP`: not tested yet |
| after a flash the bootloader **did not accept** | nothing starts it, not even a power cycle or Klipper's loader check: there is no valid app. Re-flash it |
| waiting | nothing: no loader timeout within 120 s |
| `0xF0 [02]` startup from the loader, no flash | ACKed (`00 f0 75`), box stayed in the loader |
| `00 0b 01` sent **addressed** to the loader box | no reply, box stayed in the loader. Only the `0xFF` broadcast works |

Trap: a box that sits in the bootloader at power-on gets its address from stock
Klipper anyway, because Klipper assigns saved UUIDs directly with `0xA0` (see
"Addresses and device types"). An **addressed** box ignores `0xA1`. To enumerate it
again, power-cycle that box **with Klipper stopped**.

## Notes / gotchas

- `status` differs between the two modes (`0xFF` app, `0x00` bootloader). Using the
  wrong one gets no response.
- `erase` targets the application region only — the bootloader survives, so an
  interrupted flash is normally recoverable by re-flashing.
- Command `0x06`/`0x07` in **app mode** are motor-control **writes** — do not send
  arbitrary commands while probing.
- The `0xA2` reply data is `[type] [mode] [UUID(12)]`: `type` `01` = CFS, `mode`
  `00` = app / `01` = loader. Verified with two boxes on the bus — each answers at
  its own address (`0x01`, `0x02`) and the `type` byte reads `01` on **both**, so it
  is a per-device type, not a running count. Broadcast `0xA2` to `0xFE` returns
  nothing from addressed boxes and a garbled frame from several unaddressed ones
  (see "Several boxes on one bus"). (Device-type/mode reading cross-checked with
  grant0013 / K2-OpenKlipper.)
- The bootloader does **not** keep the app-mode address; it must be re-enumerated
  (single box) or re-addressed by UUID with `0xA0` (several boxes) after entering it.

## Firmware naming and `version.json`

CFS firmware files are named `<boot_ver>-<app_ver>.bin`, e.g.
`cfs0_050_G32-cfs0_000_142.bin`:

- `boot_ver` = `cfs0_050_G32` — bootloader/hardware id; the `G32`/`G30` suffix is the
  **hardware variant** (flashing the wrong one can brick the box).
- `app_ver` = `cfs0_000_142` — the application; the trailing 3 digits are the version
  (`142` → 1.4.2, `150` → 1.5.0).

The printer keeps these under `/usr/share/klipper/fw/cfs/` with a `version.json`
that lists the bundled `boot_ver`/`app_ver` pairs. This file is only consumed by the
printer's own auto-flash mechanism (`S13mcu_update` → `mcu_util_485`); an independent
flasher reads the version off the CFS directly and does not need it. Newer bundles
have started listing an additional `cfs6`-family entry
(`boot_ver: cfs6_100_G31`, `app_ver: cfs6_220_000`), i.e. a separate, newer CFS
hardware generation with its own naming scheme — out of scope for the `cfs0` flow above.
