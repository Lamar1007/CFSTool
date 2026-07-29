# Creality CFS — RS485 protocol notes

Interoperability notes for talking to a Creality CFS over its RS485 bus. Compiled
from observing live traffic and from analyzing Creality's `mcu_util_485` for the
purpose of writing an independent, compatible flasher. Bus/command layout was
cross-checked against the archworks.co K2-Plus reverse-engineering write-up.

## Physical layer

- Half-duplex **RS485**, **230400** baud, **8N1**.
- On K1-series printers the CFS hangs off a **CH340 USB-serial adapter** (USB VID
  `1a86`), i.e. `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` → `ttyUSB0`.

## Frame format

```
[0xF7] [addr] [length] [status] [cmd] [data ...] [crc8]
```

- `0xF7` — sync/head byte.
- `addr` — device address (`0x01` = CFS box after enumeration; `0xFE` = broadcast).
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
| 0xA2 | identity/online → count + 12-byte UUID |

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

## Notes / gotchas

- The bootloader does **not** keep the app-mode address; it must be re-enumerated
  and re-assigned to `0x01` after entering the bootloader.
- `status` differs between the two modes (`0xFF` app, `0x00` bootloader). Using the
  wrong one gets no response.
- `erase` targets the application region only — the bootloader survives, so an
  interrupted flash is normally recoverable by re-flashing.
- Command `0x06`/`0x07` in **app mode** are motor-control **writes** — do not send
  arbitrary commands while probing.

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
