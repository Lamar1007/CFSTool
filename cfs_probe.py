#!/usr/bin/env python3
# cfs_probe.py  (v3)  -  READ-ONLY diagnostic for the CFS bus.
#
# Shows which CFS boxes are on the RS485 bus, at which address, in which mode
# (app or bootloader), with which UUID and firmware version. Works with one or
# several boxes connected.
#
# It sends ONLY read queries:
#   ff 14       = GET_VERSION_SN (version + serial, app mode only)
#   ff a2       = identity: device type, mode (00 app / 01 loader), 12-byte UUID
#   ff a2 @0xFE = the same as a broadcast (several boxes answer at once and
#                 collide, so expect a garbled frame there - that is normal)
#   00 a1 fe fe = enumerate broadcast (only boxes WITHOUT an address answer)
# NO load, motor, cut, reset or flash commands. Read-only.
#
# Usage:
#   python3 cfs_probe.py                 # port auto-detected (CH340)
#   python3 cfs_probe.py /dev/ttyUSB0    # explicit port
#
# IMPORTANT: stop stock Klipper first (otherwise its box service holds the port).
# Do NOT power-cycle the boxes before running this: as long as they have power
# they keep the addresses (0x01, 0x02, ...) that Klipper gave them.
# Needs pyserial (the printer's Python has it).
import sys
import glob
import time

try:
    import serial
except ImportError:
    print("ERROR: pyserial is missing. Install it:  python3 -m pip install pyserial --break-system-packages")
    sys.exit(1)

MAX_BOXES = 4


def crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
    return crc


def build_frame(addr, payload):
    body = bytes([len(payload) + 1]) + bytes(payload)
    return bytes([0xf7, addr]) + body + bytes([crc8(body)])


def hx(b):
    return ' '.join('%02x' % x for x in b)


def find_port():
    # The CFS bus is the exact "USB_Serial-if00" device. Some printers have a
    # second 1a86 adapter ("USB_Single_Serial") that is NOT the CFS.
    for pat in ('/dev/serial/by-id/usb-1a86_USB_Serial-if00*',
                '/dev/serial/by-id/*USB_Serial*', '/dev/ttyUSB*'):
        g = sorted(glob.glob(pat))
        if g:
            return g[0]
    return None


def parse_frames(buf):
    frames, i = [], 0
    while i < len(buf):
        if buf[i] != 0xf7:
            i += 1
            continue
        if i + 3 > len(buf):
            break
        total = buf[i + 2] + 3
        if i + total > len(buf):
            break
        frames.append(buf[i:i + total])
        i += total
    return frames


def send_and_read(ser, addr, payload, label, wait=1.0):
    frame = build_frame(addr, payload)
    print("\n>>> %s  (addr=0x%02x)  TX: %s" % (label, addr, hx(frame)))
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    buf = b''
    t_end = time.time() + wait
    while time.time() < t_end:
        buf += ser.read(256)
        time.sleep(0.01)
    if not buf:
        print("    (no answer)")
        return []
    print("    RX raw (%d): %s" % (len(buf), hx(buf)))
    good = []
    for f in parse_frames(buf):
        ok = (f[-1] == crc8(f[2:-1]))
        print("    frame: addr=0x%02x payload=[%s] crc=%s"
              % (f[1], hx(f[3:-1]), "ok" if ok else "BAD"))
        if ok:
            good.append(f)
    return good


def ascii_of(b):
    return ''.join(chr(x) if 32 <= x < 127 else '' for x in b)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    if not port:
        print("ERROR: no CH340 port found. Give it explicitly, e.g. python3 cfs_probe.py /dev/ttyUSB0")
        sys.exit(1)
    print("Port:", port)
    try:
        ser = serial.Serial(port, 230400, timeout=0, exclusive=True)
    except Exception as e:
        print("ERROR opening the port (%s) -> busy? Stop stock Klipper first." % e)
        sys.exit(1)

    boxes = {}       # addr -> dict(type, mode, uuid, version)
    unaddressed = []
    # 1) Every box address: version (app mode only) + identity
    for addr in range(1, MAX_BOXES + 1):
        ver = None
        for f in send_and_read(ser, addr, [0xff, 0x14], "GET_VERSION_SN  box 0x%02x" % addr):
            pl = f[3:-1]
            if len(pl) >= 2 and pl[1] == 0x14:
                ver = ascii_of(pl[2:])
        for f in send_and_read(ser, addr, [0xff, 0xa2], "identity ff a2  box 0x%02x" % addr):
            pl = f[3:-1]
            if len(pl) >= 16 and pl[1] == 0xa2:
                boxes[addr] = dict(type=pl[2], mode=pl[3], uuid=pl[4:16], version=ver)
    # 2) Identity as a broadcast (collides when several boxes answer at once)
    send_and_read(ser, 0xfe, [0xff, 0xa2], "identity ff a2  broadcast 0xFE")
    # 3) Enumerate: only boxes without an address answer, one after another
    for f in send_and_read(ser, 0xfe, [0x00, 0xa1, 0xfe, 0xfe],
                           "enumerate 00 a1 fe fe  broadcast 0xFE", wait=1.5):
        pl = f[3:-1]
        if len(pl) >= 16 and pl[1] == 0xa1:
            unaddressed.append(dict(type=pl[2], mode=pl[3], uuid=pl[4:16]))

    ser.close()

    print("\n================ SUMMARY ================")
    if not boxes and not unaddressed:
        print("No CFS answered. Is stock Klipper stopped (port free)? Is the CFS")
        print("powered and connected? If a box sits in the bootloader without an")
        print("address, power-cycle it with Klipper stopped and run this again.")
        return
    for addr in sorted(boxes):
        b = boxes[addr]
        mode = "app" if b["mode"] == 0 else ("BOOTLOADER" if b["mode"] == 1 else "0x%02x" % b["mode"])
        print("  box 0x%02x: type %02x  mode %-10s uuid %s" % (addr, b["type"], mode, hx(b["uuid"])))
        if b["version"]:
            print("            version/SN %s  (first 3 digits = firmware, e.g. 153 = 1.5.3)" % b["version"])
        elif b["mode"] == 1:
            print("            in the bootloader: no app version. If this box does not")
            print("            come back, see README 'A CFS stuck in the bootloader'.")
    for u in unaddressed:
        mode = "app" if u["mode"] == 0 else ("BOOTLOADER" if u["mode"] == 1 else "0x%02x" % u["mode"])
        print("  no address: type %02x  mode %-10s uuid %s" % (u["type"], mode, hx(u["uuid"])))
    if unaddressed:
        print("\nBoxes without an address get one when stock Klipper starts (~30 s).")
    print("\ntype 01 = CFS.  Nothing was written.")


if __name__ == '__main__':
    main()
