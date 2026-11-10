# CFS firmware changes (G32, `cfs0` family)

What changed between the official CFS firmware versions, as far as it can be read
from the images themselves. Creality publishes no changelog for the CFS, so this
was reconstructed by comparing the `.bin` files (see [How this was done](#how-this-was-done)).

**Read this as "what is visible in the binaries", not as an official changelog.**
New log messages and command names show *where* the code changed. What a change
does in detail is only stated where the machine code was actually traced; those
spots are marked **(traced)**.

| Version | Build date | Size (bytes) | In one line |
|---|---|---|---|
| 1.1.3 | 2025-01-06 | 151 724 | Factory version of early units; reads RFID tags |
| 1.4.2 | 2026-04-16 | 174 820 | Reworked RFID: new reader driver, secure chip, usage written back to the tag; factory test suite |
| 1.5.0 | 2026-06-17 | 175 104 | Per-channel logging around feeding / buffer |
| 1.5.2 | 2026-07-30 | 175 672 | New "feed a set distance" mode for the out-feed command |
| 1.5.3 | 2026-09-02 | 175 672 | One value in the factory test modes (99 → 50); normal use unchanged |

All five images share the same layout: ARM Cortex-M (GD32F303), loaded at
`0x08003000` behind the bootloader, version marker `cfs0_000_<ver>` at offset
`0x200`. The G30 and G32 images of 1.4.2 are byte-identical.

Which bus commands each version accepts (with their exact lengths, the reply
status codes and the load/unload stages) is in PROTOCOL.md, section
[Inside the app firmware (1.5.3)](PROTOCOL.md#inside-the-app-firmware-153),
table "Command set by firmware version". **(traced)**: 0x5B/0x5C/0x5D first
appear in 1.4.2, 0x12 in 1.5.2; 0x16 is in none of them.

---

## 1.5.3 (2026-09-02)

**For printing: no change.** Only 31 bytes differ from 1.5.2:

- 29 bytes are bookkeeping: the version number (stored in five places), two bytes
  right after the version marker (most likely a checksum), and the build date/time.
- **2 bytes are a real change (traced):** the mode-switch function (log
  `switch mode:%d`) passes a parameter of **99** to two factory test routines; 1.5.3
  passes **50**.
  - Mode 1 starts the **aging test** (`box_aging_mode_test [first_channel]
    [end_channel] [times]`, ends with `box aging finish all channel`), an endurance
    run over the channels used in production.
  - Mode 2 starts a second test routine over all four channels with the same value.
  - Mode 0 is normal operation (`switch standard mode!`) and is untouched.

Most likely the number of test cycles was halved to shorten factory testing. The
code shows that the value is only used by these two test modes; it does not show
for certain what the unit of the value is.

## 1.5.2 (2026-07-30)

**New: a second mode for the stage-1 out-feed command (traced).** The RS485 command
`0x12` (stage 1 of pushing filament out of the box towards the printer) now carries
a mode byte:

- **mode 0**: the previous stage-1 behaviour.
- **mode 1 (new)**: feed a **set distance at a set speed**. The request carries a
  speed byte and a 16-bit distance in mm. The box then:
  - caps the distance at **3000 mm**,
  - checks the measuring wheel (odometer) about every 20 ms and stops when the
    distance is reached (`feeding dist arrival`),
  - treats less than 0.01 mm of movement over 100 checks in a row (about **2 s**)
    as a stall and stops,
  - gives up after 1250 checks (about **25 s**) at most,
  - reports back with a `0x12` reply. Any other mode value is answered with
    `illegal data`.

New log lines: `cmd_parse_out_stage1_mode3`, `cmd_feed_process_3`,
`%s mode: %d, speed:%d, feed:%dmm`, `%s mode: %d end`, `feeding dist arrival`.

Whether stock K1 printer firmware ever sends mode 1 is unknown; a printer that only
sends the old request keeps getting the old behaviour. It looks like groundwork for
a host feature such as purge or load-length control by distance.

The size grew by 568 bytes, consistent with this one new code path.

## 1.5.0 (2026-06-17)

**Feeding / buffer diagnostics per channel.** The visible changes are log messages
in the feeder and buffer code:

- new: `[%d] ch[%d] printf feeder start` / `… feeder stop`, so feeder start/stop is
  now logged per channel,
- `buffer free!` became `[%d]buffer free!`, now with a channel index.

The size grew by only 284 bytes. Behaviour changes inside the feeding code (timing,
thresholds) are possible but are not visible without tracing that code; the
strings point to tuning of per-channel feeding and buffer handling.

## 1.4.2 (2026-04-16)

The big step: +23 096 bytes (+15 %), 104 new log/command strings.

**RFID reworked.** 1.1.3 can already detect, authenticate and read RFID tags (see
below). 1.4.2 rebuilds this and adds writing to the tag:

- New RFID reader driver (`rfid_init_info_read_fm176xx`, `rfid_detect_tag`,
  `set_tag_reader`, per-block authentication `card %d auth block %d failed`).
- Secure crypto chip support: `hardware version: secure encryption chip fm15l013`
  or `No secure encryption chip`, plus a self-test (`DES Algorithm Test`,
  `ecb enc model passed/failed`).
- Spool usage tracking written back to the tag: `rfid write used=%dmm,percent=%d`,
  `used_mm`, `total_mm`, `percent`, tag life counters (`get_tag_life_count`,
  `write used sw` / `write unused sw`), filament length at start/end of a job
  (`start_rfid_fil_len`, `end_rfid_fil_len`).
- An RFID task with states idle / print / back / save (`rfid_mgr_fila_*`) and
  `fila save:CH%d,NUM=%d,ID=…` per channel.

So writing the used length back to the spool's tag starts with 1.4.2. The printer
side has to ask for the tag data (`BOX_GET_RFID`, see README, "Two layers").

**Feeding**

- New feed process (`cmd_feed_process_2 mode: %d`), a new stage-1 parser
  (`cmd_parse_out_stage1`), a `stage6: %d.%02dmm` log line,
  per-channel distance/usage logging, and `fil_len_per_rotation`.
- Safety: `pretension now, not allow feeding!` (no feeding while the filament is
  under pretension).

**Factory / service tests**

- A main-board test mode (`main_pcb_test_mode`) with GPIO, RFID, EEPROM, BDC and
  BLDC motor, odometer (magnetic) and temperature/humidity tests, listing test points
  (`PIN_LIMIT_1 … TP42/TP28` etc.).
- Aging tests for the feeder (`feeder aging finish all channel`) and for the top
  photoelectric sensor (`print_top_PE_aging`). The mode switch (`switch mode:%d`)
  and the box aging test that 1.5.3 later touches already exist in 1.1.3.

**Removed**

- The `aging_photoelectric_CMD [first_id] [end_id]` shell command. The new
  photoelectric aging test above most likely takes its place.

## 1.1.3 (2025-01-06)

The factory version on early CFS units and the baseline for the comparison. It
already has RFID **reading**: tag detection (`rfid has a card`, `==detect multi
cards`, `different rfid uid detected, retry ...`), authentication
(`RFID_authentication`) and a per-slot read with remaining-filament estimation
(`slot%d read rfid and margin estimation`). Writing usage back to the tag and the
secure-chip support arrive in 1.4.2. It also already has the mode switch and the
box aging test.

---

## How this was done

All images are the official Creality files from a printer's
`/usr/share/klipper/fw/cfs/` folder (not included in this repo).

1. **Size, build date, version marker** read straight from each image.
2. **String comparison** between consecutive versions: printable, zero-terminated
   strings (log messages, command names) were extracted and diffed, filtering out
   byte runs that only look like text and strings that merely moved.
3. **Byte comparison** where versions are close: 1.5.2 → 1.5.3 differ in 31 bytes,
   each of which was inspected.
4. **Disassembly** (Thumb-2, image base `0x08003000`, derived from where the code's
   pointers to its own strings land) for the two spots marked **(traced)**: the
   mode-switch function in 1.5.3 and the new out-feed mode in 1.5.2.

Limits: a raw byte diff between rebuilt firmware always looks huge (addresses
shift), so it says little about how much really changed. String diffs show *where*
work happened, not every behaviour change. Anything not marked **(traced)** is an
interpretation of the strings.
