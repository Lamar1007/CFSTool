#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# cfstool.py - Talk to and flash a Creality CFS (Creality Filament System)
#              directly from a K1-series printer over RS485, in pure Python.
#
# No mcu_util_485 binary required. Works on the printer's native Python
# (uses only the standard library: os, termios), so it is CPU-architecture
# independent (runs on the MIPS printer, on a Pi, on a PC - anywhere Python
# and the serial adapter exist).
#
# SPDX-License-Identifier: MIT
#
# Note: the repo author is not a programmer - this was built with the help of AI
# (Anthropic's Claude and OpenAI's ChatGPT), reverse-engineering the stock
# mcu_util_485 and verifying every step against live hardware. Community review
# and fixes are welcome.
#
# ############################################################################
#  !!! WARNING - THE `flash` SUBCOMMAND CAN PERMANENTLY DAMAGE (BRICK) YOUR CFS
#  !!! Read the README. Use a DRY RUN first. Connect ONLY ONE CFS. Keep power
#  !!! rock-stable during writing. This is a reconstructed tool, NOT Creality's.
# ############################################################################
#
# Subcommands:
#   query   - APP-mode read only (comm test, identity, version, hw status). SAFE.
#   probe   - BOOTLOADER read only (enter-bl, enumerate, version, sector). SAFE
#             (no erase/write); startup returns to the app.
#   flash   - Flash a firmware .bin. DRY RUN by default; add --flash to write.
#
# Examples:
#   python3 /usr/data/cfstool.py query
#   python3 /usr/data/cfstool.py diag      (tidy diagnostics)
#   python3 /usr/data/cfstool.py probe
#   python3 /usr/data/cfstool.py flash                                       # dry run
#   python3 /usr/data/cfstool.py flash --flash    # real
#   python3 /usr/data/cfstool.py query --dev /dev/ttyUSB0
#
import os, sys, termios, time, math, json

DEFAULT_DEV = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
BAUD = 230400
BOX_ADDR = 0x01          # the CFS box address after enumeration
BROADCAST = 0xFE
DEFAULT_FW_DIR = "/usr/share/klipper/fw/cfs"   # where the printer keeps CFS firmware

# --------------------------------------------------------------------------
# Wire protocol
#   frame: [0xF7][addr][length][status][cmd][data...][crc8]
#   length = len(data) + 3   (status + cmd + crc)
#   crc8: poly 0x07 over [length, status, cmd, data...]  (excludes 0xF7 + addr)
#   status: 0xFF for APP-mode requests, 0x00 for bootloader-mode requests
# --------------------------------------------------------------------------
def crc8(data, poly=0x07):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc & 0xFF

def build(addr, cmd, data=b"", status=0x00):
    length = len(data) + 3
    body = bytes([length, status, cmd]) + data
    return bytes([0xF7, addr]) + body + bytes([crc8(body)])

def parse(resp):
    i = resp.find(b"\xf7")
    if i < 0 or len(resp) - i < 6:
        return None
    r = resp[i:]; length = r[2]
    return dict(addr=r[1], status=r[3], cmd=r[4],
                data=r[5:5 + max(0, length - 3)], raw=r)

# --------------------------------------------------------------------------
# Serial (pure termios, no pyserial)
# --------------------------------------------------------------------------
def open_port(dev, baud=BAUD):
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = 0; a[1] = 0
    a[2] = termios.CS8 | termios.CLOCAL | termios.CREAD
    a[3] = 0
    spd = getattr(termios, "B%d" % baud)
    a[4] = spd; a[5] = spd
    a[6] = a[6][:]; a[6][termios.VMIN] = 0; a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    os.set_blocking(fd, False)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd

def _complete(b2):
    i = b2.find(b"\xf7")
    if i >= 0 and len(b2) - i >= 3:
        length = b2[i + 2]
        if len(b2) - i >= 3 + length:
            return b2[i:i + 3 + length]
    return None

def xfer(fd, pkt, wait=0.5):
    """Send pkt, return the first complete response frame (echo removed),
    or whatever arrived within `wait` seconds."""
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, pkt)
    buf = b""; t0 = time.time()
    while time.time() - t0 < wait:
        try:
            c = os.read(fd, 256)
        except (BlockingIOError, OSError):
            c = b""
        if c:
            buf += c
            b2 = buf[len(pkt):] if buf.startswith(pkt) else buf
            fr = _complete(b2)
            if fr is not None:
                return fr
        else:
            time.sleep(0.004)
    return buf[len(pkt):] if buf.startswith(pkt) else buf

def step(fd, name, pkt, tries=3, wait=0.6, verbose=True):
    """Send a command, expect a response (= ACK). Up to `tries` attempts."""
    for k in range(tries):
        resp = xfer(fd, pkt, wait)
        p = parse(resp)
        if p is not None:
            if verbose:
                print("  %-22s OK   RX %s" % (name, resp.hex(" ")))
            return p
        if verbose:
            print("  %-22s ...  no ACK, retry %d" % (name, k + 1))
    print("  %-22s FAIL: no ACK after %d tries" % (name, tries))
    return None

# --------------------------------------------------------------------------
# Bootloader sequence (reconstructed from Creality's mcu_util_485)
# --------------------------------------------------------------------------
ENTER_BOOTLOADER = bytes.fromhex("f7eb03ff56cf")   # broadcast: enter IAP/bootloader

def enter_bootloader(fd):
    print("  enter bootloader       TX %s" % ENTER_BOOTLOADER.hex(" "))
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, ENTER_BOOTLOADER)
    time.sleep(1.0)

def enumerate_and_assign(fd):
    """Enumerate the CFS on the bus and give it address 0x01. Returns UUID or None."""
    pe = step(fd, "enumerate 0xA1", build(BROADCAST, 0xA1, b"\xfe\xfe"), wait=1.2)
    if not pe or len(pe["data"]) < 12:
        return None
    uuid = pe["data"][-12:]
    print("  UUID: %s" % uuid.hex(" "))
    pa = step(fd, "assign 0x01 0xA0", build(BROADCAST, 0xA0, b"\x01" + uuid), wait=1.5)
    return uuid if pa else None

def bl_get_version(fd):
    p = step(fd, "get version 0xF0[00]", build(BOX_ADDR, 0xF0, b"\x00"))
    if not p:
        return None
    return "".join(chr(b) if 32 <= b < 127 else "" for b in p["data"])

def bl_get_sector(fd):
    p = step(fd, "get sector 0xF0[03]", build(BOX_ADDR, 0xF0, b"\x03"))
    if not p or not p["data"]:
        return None
    return p["data"][0]

def bl_startup(fd, verbose=True):
    return step(fd, "startup 0xF0[02]", build(BOX_ADDR, 0xF0, b"\x02"), verbose=verbose)

# --------------------------------------------------------------------------
# Subcommand: query  (APP mode, read only)
# --------------------------------------------------------------------------
def cmd_query(dev):
    print("Port:", dev, "@", BAUD, "8N1   (APP-mode reads only)\n")
    fd = open_port(dev)
    try:
        for name, cmd in [("COMMUNICATION_TEST", 0x55), ("Identity 0xA2", 0xA2),
                          ("GET_VERSION_SN 0x14", 0x14), ("GET_HW_STATUS 0x15", 0x15)]:
            r = xfer(fd, build(BOX_ADDR, cmd, status=0xFF))
            p = parse(r)
            print("  %-20s RX %s" % (name, r.hex(" ") if r else "(no answer)"))
            if p:
                ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in p["data"])
                print("  %-20s    data=%s  ascii=%r" % ("", p["data"].hex(" "), ascii_))
            print()
    finally:
        os.close(fd)
    print("Done. Nothing was written.")

# --------------------------------------------------------------------------
# Subcommand: diag  (APP mode, read only - nicely categorized diagnostics)
#   Sends ONLY safe read commands. Motor/environment-sensor commands are
#   deliberately NOT sent (they are ambiguous/side-effecting in the protocol),
#   consistent with "never send an unverified command to the CFS".
# --------------------------------------------------------------------------
def cmd_diag(dev):
    print("Port:", dev, "@", BAUD, "8N1   (diagnostics, APP-mode reads only)\n")
    fd = open_port(dev)

    def rd(cmd, data=b""):
        return parse(xfer(fd, build(BOX_ADDR, cmd, data, status=0xFF)))

    try:
        # Communication
        print("  %-14s: %s" % ("Kommunikation", "OK (ACK)" if rd(0x55) else "no answer"))

        # Firmware + serial (GET_VERSION_SN 0x14): "<3 digits version><serial>"
        p = rd(0x14)
        if p:
            s = "".join(chr(b) if 32 <= b < 127 else "" for b in p["data"])
            if len(s) >= 3 and s[:3].isdigit():
                print("  %-14s: %s.%s.%s" % ("Firmware", s[0], s[1], s[2]))
                print("  %-14s: %s" % ("Seriennummer", s[3:] or "(none)"))
            else:
                print("  %-14s: %r" % ("Firmware", s))
        else:
            print("  %-14s: no answer" % "Firmware")

        # UUID + device count (Identity 0xA2)
        p = rd(0xA2)
        if p and len(p["data"]) >= 12:
            print("  %-14s: %s" % ("UUID", p["data"][-12:].hex(" ")))
        else:
            print("  %-14s: no answer" % "UUID")

        # Hardware status (0x15) - raw status bytes
        p = rd(0x15)
        print("  %-14s: %s" % ("Hardware-Stat", p["data"].hex(" ") if p else "no answer"))

        # RFID / filament per slot (GET_RFID 0x02, slot_mask 0x0F = all).
        # Documented read; harmless (queries the slot RFID readers).
        p = rd(0x02, b"\x0f")
        if p:
            txt = "".join(chr(b) if 32 <= b < 127 else "." for b in p["data"])
            print("  %-14s: %s" % ("RFID/Slots", txt or "(empty)"))
        else:
            print("  %-14s: (no answer / no tags)" % "RFID/Slots")
    finally:
        os.close(fd)

    print()
    print("  Hardware variant + bootloader:  run  cfstool.py probe")
    print("  (that reads the full boot_ver, which needs bootloader mode).")
    print("  Motor state and environment sensors (temp/humidity, filament")
    print("  sensors) are NOT queried on purpose: those commands are not")
    print("  clean read-only in the protocol, and this tool never sends an")
    print("  unverified command to the CFS. Nothing was written.")

# --------------------------------------------------------------------------
# Subcommand: probe  (BOOTLOADER mode, read only - no erase/write)
# --------------------------------------------------------------------------
def cmd_probe(dev):
    print("Port:", dev, "@", BAUD, "8N1")
    print(">>> SAFE PROBE: no erase, no write. <<<\n")
    fd = open_port(dev)
    try:
        enter_bootloader(fd)
        if not enumerate_and_assign(fd):
            print("\nNo device enumerated. Is exactly one CFS connected / Klipper stopped?")
            return
        print()
        print("  -> version:", repr(bl_get_version(fd)))
        sect = bl_get_sector(fd)
        if sect is not None:
            print("  -> sector 0x%02x  => chunk = %d bytes" % (sect, (sect * 0xFC) & 0xFF))
        bl_startup(fd)
    finally:
        os.close(fd)
    print("\nDone. Nothing was written. If the CFS acts odd: power-cycle it.")

# --------------------------------------------------------------------------
# Subcommand: flash  (DRY RUN default; --flash writes)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Firmware discovery / selection
# --------------------------------------------------------------------------
def fmt_ver(app_ver):
    """'cfs0_000_142' -> '1.4.2'; unknown schemes returned as-is."""
    tok = app_ver.split("_")[-1]
    if len(tok) == 3 and tok.isdigit():
        return "%s.%s.%s" % (tok[0], tok[1], tok[2])
    return app_ver or "?"

def split_bootapp(s):
    """'cfs0_050_G32-cfs0_000_142' -> ('cfs0_050_G32', 'cfs0_000_142')."""
    p = s.split("-")
    return (p[0], p[1]) if len(p) >= 2 else (s, "")

def hw_tag(boot_ver):
    """'cfs0_050_G32' -> 'G32' (the hardware identifier)."""
    return boot_ver.split("_")[-1] if boot_ver else ""

def scan_firmware(dirpath):
    """Return a list of CFS firmware .bin files found in dirpath."""
    out = []
    try:
        for fn in sorted(os.listdir(dirpath)):
            if fn.lower().endswith(".bin") and fn.startswith("cfs"):
                boot, app = split_bootapp(fn[:-4])
                out.append(dict(path=os.path.join(dirpath, fn), name=fn,
                                boot=boot, app=app, ver=fmt_ver(app)))
    except OSError:
        pass
    return out

def choose_firmware(cands, cur_boot, cur_ver):
    """Print the candidate list (marking compatibility) and prompt for a choice.
    Returns the chosen candidate dict, or None to abort."""
    cur_hw = hw_tag(cur_boot)
    print("\nAvailable firmware (connected CFS: hardware %s, version %s):\n"
          % (cur_hw or "?", cur_ver or "?"))
    for i, c in enumerate(cands):
        compat = (hw_tag(c["boot"]) == cur_hw) if cur_hw else True
        same = (c["app"] == split_bootapp(cur_ver)[1]) if cur_ver else False
        tag = "  " if compat else "! "     # ! = wrong hardware
        note = ""
        if not compat:
            note = "  <- WRONG HARDWARE (%s), do not pick" % hw_tag(c["boot"])
        elif same:
            note = "  <- already installed"
        print("  %s[%d] %-34s  v%s%s" % (tag, i, c["name"], c["ver"], note))
    print()
    try:
        sel = input("Which firmware to install? [number, or q to cancel]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(); return None
    if sel.lower() in ("q", "quit", "", "c", "cancel"):
        return None
    if not sel.isdigit() or int(sel) >= len(cands):
        print("Invalid selection."); return None
    return cands[int(sel)]

def preview_version_json(fw_dir, boot_ver, app_ver):
    """Report what update_version_json would change (dry run), without writing."""
    jpath = os.path.join(fw_dir, "version.json")
    if not os.path.isfile(jpath):
        print("  version.json: none in %s (would be left untouched)" % fw_dir); return
    try:
        data = json.load(open(jpath))
    except Exception:
        print("  version.json: present but unreadable (would be left untouched)"); return
    cur = None
    for e in data.get("CFSs", []):
        if e.get("boot_ver") == boot_ver:
            cur = e.get("app_ver")
    if cur == app_ver:
        print("  version.json: %s already %s (no change needed)" % (boot_ver, app_ver))
    elif cur is None:
        print("  version.json: would ADD %s -> %s (with .bak backup)" % (boot_ver, app_ver))
    else:
        print("  version.json: would update %s  %s -> %s (with .bak backup)"
              % (boot_ver, cur, app_ver))

def update_version_json(fw_dir, boot_ver, app_ver):
    """Back up version.json (timestamped) and set the given boot_ver's app_ver.
    Preserves all other entries. Safe: never touches the file without a backup."""
    jpath = os.path.join(fw_dir, "version.json")
    if not os.path.isfile(jpath):
        print("  version.json: none in %s - skipping display-version update." % fw_dir)
        return
    try:
        data = json.load(open(jpath))
    except Exception as ex:
        print("  version.json unreadable (%s) - leaving it untouched." % ex); return
    bak = "%s.bak_%s" % (jpath, time.strftime("%Y-%m-%d_%H%M%S"))
    try:
        with open(bak, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as ex:
        print("  could NOT write backup (%s) - leaving version.json untouched." % ex); return
    print("  backed up version.json -> %s" % os.path.basename(bak))
    cfss = data.get("CFSs", [])
    found = False
    for e in cfss:
        if e.get("boot_ver") == boot_ver:
            e["app_ver"] = app_ver; found = True
    if not found:
        cfss.append({"boot_ver": boot_ver, "app_ver": app_ver})
        data["CFSs"] = cfss
    try:
        with open(jpath, "w") as f:
            json.dump(data, f, indent=2)
        print("  updated version.json: %s -> %s%s"
              % (boot_ver, app_ver, "" if found else "  (added)"))
    except Exception as ex:
        print("  could NOT write version.json (%s). Original backup: %s" % (ex, bak))

# --------------------------------------------------------------------------
# Subcommand: flash  (DRY RUN default; --flash writes)
#   path may be a .bin file, a directory to scan, or None (-> DEFAULT_FW_DIR)
# --------------------------------------------------------------------------
def cmd_flash(dev, path, do_flash, update_json=True):
    # Decide firmware source: single file, or a directory to scan + choose from.
    single = path if (path and os.path.isfile(path)) else None
    if not single:
        scandir = path if (path and os.path.isdir(path)) else DEFAULT_FW_DIR
        cands = scan_firmware(scandir)
        if not cands:
            print("No cfs*.bin firmware found in: %s" % scandir)
            print("Give a file or directory:  cfstool.py flash <path> [--flash]")
            return
        print("Scanned %s: found %d firmware file(s)." % (scandir, len(cands)))

    print("\nMode: %s\n" % ("!!! REAL FLASH (erase/write) !!!"
                            if do_flash else "DRY RUN (no erase/write)"))
    fd = open_port(dev)
    try:
        enter_bootloader(fd)
        if not enumerate_and_assign(fd):
            print("ABORT: enumeration/addressing failed."); return
        print()
        ver = bl_get_version(fd)
        print("  -> current version: %r" % ver)
        sect = bl_get_sector(fd)
        if sect is None:
            print("ABORT: no sector size."); return
        chunk = (sect * 0xFC) & 0xFF
        print("  -> sector 0x%02x  => chunk = %d bytes" % (sect, chunk))
        if not ver or "cfs0" not in ver:
            print("ABORT: unexpected/empty version -> not flashing."); return
        cur_boot, cur_app = split_bootapp(ver)

        # Pick the firmware
        if single:
            boot, app = split_bootapp(os.path.basename(single)[:-4])
            chosen = dict(path=single, name=os.path.basename(single),
                          boot=boot, app=app, ver=fmt_ver(app))
        else:
            chosen = choose_firmware(cands, cur_boot, ver)
            if not chosen:
                print("Cancelled - nothing written.")
                bl_startup(fd, verbose=False); return

        # Load + validate the chosen file
        try:
            fw = open(chosen["path"], "rb").read()
        except Exception as e:
            print("ERROR: cannot read .bin:", e); return
        fw_len = len(fw)
        print("\nSelected: %s  (v%s)" % (chosen["name"], chosen["ver"]))
        print("  size: %d bytes" % fw_len)

        # ---- safety gates ----
        if b"cfs0_000_" not in fw:
            print("ABORT: file has no 'cfs0_000_' marker - not a CFS firmware .bin.")
            bl_startup(fd, verbose=False); return
        if hw_tag(chosen["boot"]) and hw_tag(cur_boot) and \
           hw_tag(chosen["boot"]) != hw_tag(cur_boot):
            print("ABORT: hardware mismatch - file is %s but this CFS is %s."
                  % (hw_tag(chosen["boot"]), hw_tag(cur_boot)))
            bl_startup(fd, verbose=False); return
        if chosen["app"] and (b"cfs0_000_" + chosen["app"].split("_")[-1].encode()) in fw \
           and chosen["app"] == cur_app:
            print("ABORT: connected CFS already runs %s." % chosen["ver"])
            bl_startup(fd, verbose=False); return
        if chunk == 0:
            print("ABORT: chunk size 0 (unexpected sector value)."); return

        nchunks = math.ceil(fw_len / chunk)
        print("PLAN: %s  ->  %s   (%d bytes, %d frames of %d, last %d)" %
              (fmt_ver(cur_app), chosen["ver"], fw_len, nchunks, chunk,
               fw_len - (nchunks - 1) * chunk))

        if not do_flash:
            if update_json:
                preview_version_json(os.path.dirname(os.path.abspath(chosen["path"])),
                                     chosen["boot"], chosen["app"])
            print("\n>>> DRY RUN complete. NOTHING was written.")
            print(">>> If the plan looks right, re-run with  --flash  to write.")
            bl_startup(fd, verbose=False)
            return

        # ================= REAL FLASH =================
        print("\n=== FLASH START - DO NOT POWER OFF ===")
        if not step(fd, "erase 0xF0[06]",   build(BOX_ADDR, 0xF0, b"\x06"), wait=5.0):
            print("ABORT at erase."); return
        if not step(fd, "request 0xF0[01]", build(BOX_ADDR, 0xF0, b"\x01")):
            print("ABORT at request update."); return
        lenb = bytes([fw_len & 0xff, (fw_len >> 8) & 0xff,
                      (fw_len >> 16) & 0xff, (fw_len >> 24) & 0xff])
        if not step(fd, "fw len 0xF0",      build(BOX_ADDR, 0xF0, lenb)):
            print("ABORT at fw len."); return

        print("  -> sending %d frames ..." % nchunks)
        off = 0; idx = 0
        while off < fw_len:
            block = fw[off:off + chunk]
            ok = False
            for _ in range(3):
                if parse(xfer(fd, build(BOX_ADDR, 0xF0, block), wait=0.6)) is not None:
                    ok = True; break
            if not ok:
                print("\nABORT: frame %d/%d had no ACK at offset %d." % (idx + 1, nchunks, off))
                print("  Flash incomplete; CFS stays in bootloader. Do NOT power off in a")
                print("  panic - the bootloader survives, just re-run the flash.")
                return
            off += len(block); idx += 1
            if idx % 50 == 0 or off >= fw_len:
                print("     %d/%d frames (%d/%d bytes)" % (idx, nchunks, off, fw_len))

        time.sleep(0.5)
        if not bl_startup(fd):
            print("WARNING: startup had no ACK - power-cycle the CFS if needed.")
        print("=== FLASH END ===\n")
        if update_json:
            print("Updating printer-side version.json (with backup):")
            update_version_json(os.path.dirname(os.path.abspath(chosen["path"])),
                                chosen["boot"], chosen["app"])
            print()
        print("Verify with:  python3 /usr/data/cfstool.py query   (after power-cycling the CFS)")
        print("Look at GET_VERSION_SN - the leading digits are the version.")
    finally:
        os.close(fd)

# --------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    dev = DEFAULT_DEV
    if "--dev" in args:
        i = args.index("--dev"); dev = args[i + 1]; del args[i:i + 2]
    do_flash = "--flash" in args
    args = [a for a in args if a != "--flash"]
    update_json = "--no-json" not in args           # default: keep version.json in sync
    args = [a for a in args if a != "--no-json"]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__ if __doc__ else "")
        print("usage: cfstool.py {query | diag | probe | flash [path]} [--flash] [--no-json] [--dev PORT]")
        print("  flash with no path scans %s and lets you pick a version." % DEFAULT_FW_DIR)
        print("  after a real flash, version.json is updated (backup kept); --no-json skips that.")
        return
    sub = args[0]
    if sub == "query":
        cmd_query(dev)
    elif sub == "diag":
        cmd_diag(dev)
    elif sub == "probe":
        cmd_probe(dev)
    elif sub == "flash":
        path = args[1] if len(args) >= 2 else None   # None -> scan DEFAULT_FW_DIR
        cmd_flash(dev, path, do_flash, update_json)
    else:
        print("unknown subcommand: %s (use query|probe|flash)" % sub)

if __name__ == "__main__":
    main()
