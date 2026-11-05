#!/usr/bin/env python3
# cfs_probe.py  (v2)  -  REINES LESE-/DIAGNOSE-Skript fuer die CFS-Adressierung.
#
# Zweck: grants offene Frage klaeren -> ist das erste Byte der "ff a2"-Antwort
# ein GERAETETYP (bleibt 01, egal wie viele Boxen) oder eine ANZAHL (steigt mit
# der 2. Box auf 02)?
#
# Es schickt NUR Lese-Abfragen:
#   ff 14  = GET_VERSION_SN   (Kommunikations-Check)
#   ff a2  = Adress-/Identitaets-Abfrage (die CFSTool-Abfrage, laut grants Doc)
#   00 a1 fe fe = Enumerate-Broadcast (Handshake, nur als letzter Versuch)
# KEINE Lade-, Motor-, Schneide-, Reset- oder Flash-Befehle. Read-only.
#
# Nutzung:
#   python3 cfs_probe.py                 # Port automatisch (CH340)
#   python3 cfs_probe.py /dev/ttyUSB0    # Port explizit
#
# WICHTIG: Stock-Klipper vorher stoppen (sonst belegt der Box-Dienst den Port).
# Die Boxen NICHT stromlos machen - solange sie Strom haben, behalten sie die
# Adressen 0x01/0x02, die Klipper ihnen zugewiesen hat.
import sys
import glob
import time

try:
    import serial
except ImportError:
    print("FEHLER: pyserial fehlt. Installieren:  python3 -m pip install pyserial --break-system-packages")
    sys.exit(1)


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
    for pat in ('/dev/serial/by-id/*1a86*USB*', '/dev/serial/by-id/*USB_Serial*', '/dev/ttyUSB*'):
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
        print("    (keine Antwort)")
        return []
    print("    RX roh (%d): %s" % (len(buf), hx(buf)))
    frames = parse_frames(buf)
    for f in frames:
        pl = f[3:-1]
        ok = (f[-1] == crc8(f[2:-1]))
        print("    Frame: addr=0x%02x payload=[%s] crc=%s" % (f[1], hx(pl), "ok" if ok else "FALSCH"))
    return frames


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    if not port:
        print("FEHLER: kein CH340-Port gefunden. Explizit angeben, z.B. python3 cfs_probe.py /dev/ttyUSB0")
        sys.exit(1)
    print("Port:", port)
    try:
        ser = serial.Serial(port, 230400, timeout=0, exclusive=True)
    except Exception as e:
        print("FEHLER beim Oeffnen (%s) -> Port belegt? Erst Stock-Klipper stoppen." % e)
        sys.exit(1)

    a2_hits = []
    # 1) Kommunikations-Check + eigentliche Abfrage an Box 1 und Box 2
    for addr in (0x01, 0x02):
        send_and_read(ser, addr, [0xff, 0x14], "GET_VERSION_SN (Komm-Check) Box 0x%02x" % addr)
        fr = send_and_read(ser, addr, [0xff, 0xa2], "ff a2 (device count/type) Box 0x%02x" % addr)
        for f in fr:
            pl = f[3:-1]
            if len(pl) >= 2 and pl[1] == 0xa2:
                a2_hits.append((addr, pl))
    # 2) ff a2 als Broadcast (falls die Anzahl nur global kommt)
    fr = send_and_read(ser, 0xfe, [0xff, 0xa2], "ff a2 Broadcast 0xFE")
    for f in fr:
        pl = f[3:-1]
        if len(pl) >= 2 and pl[1] == 0xa2:
            a2_hits.append((0xfe, pl))
    # 3) Enumerate-Handshake (nur falls oben alles leer blieb)
    send_and_read(ser, 0xfe, [0x00, 0xa1, 0xfe, 0xfe], "Enumerate 00 a1 fe fe (Handshake)")

    ser.close()

    print("\n================ AUSWERTUNG ================")
    if not a2_hits:
        print("Keine a2-Antwort erkannt. Bitte die KOMPLETTE Ausgabe oben an Claude schicken.")
        print("(Kam auch bei 'GET_VERSION_SN' nichts, ist der Port noch belegt oder die")
        print(" Box nicht adressiert - dann Boxen einmal stromlos + neu und erneut.)")
        return
    for addr, pl in a2_hits:
        x = pl[2] if len(pl) > 2 else 0
        rest = pl[3:] if len(pl) > 3 else b''
        print("  von 0x%02x:  erstes Byte nach a2 = 0x%02x   (Rest: %s)" % (addr, x, hx(rest)))
    print("\nMit BEIDEN Boxen angeschlossen:")
    print("  Byte = 01 (bzw. jede Box meldet 01) -> GERAETETYP (grant hat recht)")
    print("  Byte = 02                           -> ANZAHL (deine Lesart war richtig)")


if __name__ == '__main__':
    main()
