# k1_cfs_bus.py - universelle CFS/Bus-Transportschicht fuer MAINLINE Klipper
# auf ALLEN K1-Geraeten (K1, K1C, K1 Max, K1 SE), S11- UND S12-Boards.
#
# Idee: Die CFS-Logik (portiert aus grant0013/K2-OpenKlipper k2_cfs.py) spricht
# nur EINE Schnittstelle:  frame, resp = bus.xfer(addr, payload, timeout=...)
# Diese Datei stellt diese Schnittstelle fuer ZWEI Transporte bereit. Die
# Befehls-/Frame-Ebene ist bei allen K1 identisch (in K1-ANALYSIS.md bewiesen
# und gegen K2 kreuzvalidiert); nur der Transport kann abweichen:
#
#   RS485 : CFS ueber CH340 -> /dev/ttyUSB0 @ 230400     [PRODUKTIV]
#   CAN   : CFS ueber can0 @ 1 Mbit/s                    [EXPERIMENTELL]
#
# WICHTIG: Der Transport haengt NICHT an der Board-Revision (S11/S12). Ein und
# dasselbe S12-Board kann RS485 fahren (bestaetigt: K1 Max mit S12-Board,
# arbeitet ueber RS485). Darum wird der Transport NICHT aus dem Board-Namen
# abgeleitet, sondern zur Laufzeit am tatsaechlich vorhandenen Geraet erkannt
# (erst /dev/ttyUSB*/CH340, dann can*). 'transport: auto' macht genau das.
#
# Gemeinsame Applikationsschicht (beide Transporte):
#   frame = 0xF7 | addr | len | payload... | crc8
#     len  = Anzahl Bytes nach dem len-Byte (payload + crc)
#     crc8 = Poly 0x07, init 0x00, unreflektiert, xorout 0x00, ueber (len+payload)
#
# Der RS485-Pfad ist abgeleitet aus grant0013s k2_motor_bus.py (GPLv3) und
# deckt sich mit dem verifizierten cfstool-Protokoll (PROTOCOL.md).
# Der CAN-Pfad ist ein belegbasiertes Geruest (aus cfs_iap_upgchck.sh und dem
# SerialCanManager in box_wrapper.pyc) und MUSS auf einem S12-Board per candump
# verifiziert werden, bevor er fuer Aktuierung genutzt wird.
#
# Lizenz: GPLv3 (wie Klipper und K2-OpenKlipper). Kein Creality-Code enthalten;
# Protokollwissen aus Beobachtung/Interop. Credit: Grant Harkness (Hark Tech).
#
# Config:
#   [k1_cfs_bus]
#   #transport: auto          # auto | rs485 | can
#   #serial: /dev/ttyUSB0     # RS485: fester Pfad; leer => Autosuche CH340
#   #baud: 230400
#   #can_interface: can0      # CAN: Interface-Name
#   #can_bitrate: 1000000

import glob
import logging
import os


# --------------------------------------------------------------------------
# Gemeinsame Frame-Helfer (transportunabhaengig)
# --------------------------------------------------------------------------
def crc8(data):
    """CRC-8, Poly 0x07, init 0x00, keine Reflexion, kein finales XOR."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
    return crc


def build_frame(addr, payload):
    """0xF7 | addr | len | payload | crc8   (len = len(payload)+1)."""
    body = bytes([len(payload) + 1]) + bytes(payload)
    return bytes([0xf7, addr]) + body + bytes([crc8(body)])


def find_cfs_serial():
    """CH340-RS485-Geraet des CFS suchen (S11-Board). Bevorzugt den stabilen
    by-id-Pfad, faellt auf /dev/ttyUSB* zurueck. Gibt Pfad oder None."""
    # Stabiler Pfad (ueberlebt Reboots/Portwechsel):
    for p in sorted(glob.glob('/dev/serial/by-id/*1a86*USB*')):
        return p
    for p in sorted(glob.glob('/dev/serial/by-id/*USB_Serial*')):
        return p
    ttys = sorted(glob.glob('/dev/ttyUSB*'))
    return ttys[0] if ttys else None


def find_can_interface():
    """Erstes UP-faehiges CAN-Interface suchen (S12-Board). Gibt Name oder None."""
    for path in sorted(glob.glob('/sys/class/net/can*')):
        return os.path.basename(path)
    return None


# --------------------------------------------------------------------------
# RS485-Backend (S11)  -- produktiv, aus grant0013 + cfstool verifiziert
# --------------------------------------------------------------------------
class _RS485Transport:
    def __init__(self, serialport, baud, reactor):
        self.serialport = serialport
        self.baud = baud
        self.reactor = reactor
        self.ser = None

    def _open(self):
        if self.ser is not None:
            return
        import serial  # pyserial
        self.ser = serial.Serial(self.serialport, self.baud,
                                 timeout=0.2, exclusive=True)
        logging.info("k1_cfs_bus: RS485 geoeffnet %s @ %d",
                     self.serialport, self.baud)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _read_frame(self, timeout, read_len):
        # Nicht-blockierend zwischen Reactor-Pausen lesen: eine CFS-Stage kann
        # 20+ s bis zur Antwort brauchen, und Klipper muss derweil die Heizung
        # weiter regeln (ein blockierter Reactor liess den Hotend driften).
        deadline = self.reactor.monotonic() + timeout
        self.ser.timeout = 0
        buf = b''
        while True:
            buf += self.ser.read(256)
            start = buf.find(b'\xf7')          # Rauschen bis zum Preamble verwerfen
            buf = buf[start:] if start >= 0 else b''
            if len(buf) >= 3:
                frame_len = buf[2]
                if frame_len < 1 or frame_len + 3 > read_len:
                    return buf[:3]
                if len(buf) >= frame_len + 3:
                    return buf[:frame_len + 3]
            now = self.reactor.monotonic()
            if now >= deadline:
                return buf
            self.reactor.pause(min(deadline, now + 0.005))

    def xfer(self, addr, payload, read_len=64, timeout=0.2):
        self._open()
        frame = build_frame(addr, payload)
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        resp = self._read_frame(timeout, read_len)
        return frame, resp


# --------------------------------------------------------------------------
# CAN-Backend (S12)  -- EXPERIMENTELL, per candump auf S12 zu verifizieren
# --------------------------------------------------------------------------
class _CANTransport:
    # Belege (K1-ANALYSIS.md): cfs_iap_upgchck.sh nutzt can0 @ 1 Mbit/s und
    # verpackt dasselbe 0xF7-Frame in CAN-Datenfelder, z.B.
    #   cansend 0FD#F7FD04FF25A4E9   (Enter-Bootloader, Cutter 0xA4)
    #   cansend 0FE#F7FE04FF25A2FB   (Enter-Bootloader, Box   0xA2)
    # Das sind IAP/Bootloader-IDs. Die LAUFZEIT-CAN-IDs des box_wrapper-
    # SerialCanManager fuer normale CFS-Frames sind noch NICHT reversed -
    # darum ist dieser Pfad ein Geruest. Vor Aktuierung: auf S12 candump
    # mitschneiden und TX-/RX-CAN-ID + Fragmentierung bestaetigen.
    TX_CAN_ID = 0x0FE   # VORLAEUFIG (Box). Per candump bestaetigen!
    RX_CAN_ID = None    # per candump bestimmen

    def __init__(self, interface, bitrate, reactor):
        self.interface = interface
        self.bitrate = bitrate
        self.reactor = reactor
        self.bus = None

    def _open(self):
        if self.bus is not None:
            return
        try:
            import can  # python-can
        except ImportError:
            raise RuntimeError(
                "CAN-Transport braucht python-can (pip install python-can) "
                "oder einen socketcan-Fallback")
        self.bus = can.interface.Bus(channel=self.interface,
                                     bustype='socketcan')
        logging.info("k1_cfs_bus: CAN geoeffnet %s", self.interface)

    def close(self):
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None

    def xfer(self, addr, payload, read_len=64, timeout=0.2):
        # GERUEST: baut dasselbe 0xF7-Frame und legt es ins CAN-Datenfeld.
        # CAN-Frames fassen max. 8 Byte Daten -> laengere CFS-Frames muessen
        # fragmentiert werden (Konvention per candump zu bestaetigen).
        self._open()
        import can
        frame = build_frame(addr, payload)
        if len(frame) > 8:
            raise NotImplementedError(
                "CAN-Fragmentierung fuer Frames > 8 Byte noch nicht "
                "verifiziert - bitte candump auf S12-Board liefern")
        msg = can.Message(arbitration_id=self.TX_CAN_ID,
                          data=frame, is_extended_id=False)
        self.bus.send(msg)
        deadline = self.reactor.monotonic() + timeout
        resp = b''
        while self.reactor.monotonic() < deadline:
            rx = self.bus.recv(timeout=0.01)
            if rx is not None and (self.RX_CAN_ID is None
                                   or rx.arbitration_id == self.RX_CAN_ID):
                resp = bytes(rx.data)
                break
            self.reactor.pause(self.reactor.monotonic() + 0.005)
        return frame, resp


# --------------------------------------------------------------------------
# Oeffentliche Bus-Klasse: waehlt/erkennt den Transport, einheitliches xfer()
# --------------------------------------------------------------------------
class K1CFSBus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.transport_name = config.get('transport', 'auto').lower()
        self.serialport = config.get('serial', '').strip()
        self.baud = config.getint('baud', 230400, minval=9600)
        self.can_interface = config.get('can_interface', '').strip()
        self.can_bitrate = config.getint('can_bitrate', 1000000, minval=10000)
        self._backend = None
        self._xfer_lock_held = False
        self.printer.register_event_handler("klippy:disconnect",
                                             self._handle_disconnect)

    # ---- Transport-Auswahl / Autoerkennung -------------------------------
    def _select_backend(self):
        if self._backend is not None:
            return self._backend
        name = self.transport_name
        if name == 'auto':
            # RS485 zuerst: existiert ein CH340-Geraet? (unabhaengig vom Board)
            ser = self.serialport or find_cfs_serial()
            if ser:
                name = 'rs485'
            elif find_can_interface():
                name = 'can'
            else:
                raise self.printer.command_error(
                    "k1_cfs_bus: kein CFS-Transport gefunden - weder "
                    "/dev/ttyUSB* (S11/RS485) noch can* (S12/CAN). Laeuft "
                    "k1-inventory.sh - was meldet Abschnitt 6?")
        if name == 'rs485':
            ser = self.serialport or find_cfs_serial()
            if not ser:
                raise self.printer.command_error(
                    "k1_cfs_bus: RS485 gewaehlt, aber kein CH340/ttyUSB "
                    "gefunden")
            self._backend = _RS485Transport(ser, self.baud, self.reactor)
            logging.info("k1_cfs_bus: Transport = RS485 (%s)", ser)
        elif name == 'can':
            iface = self.can_interface or find_can_interface()
            if not iface:
                raise self.printer.command_error(
                    "k1_cfs_bus: CAN gewaehlt, aber kein can-Interface "
                    "gefunden")
            self._backend = _CANTransport(iface, self.can_bitrate,
                                          self.reactor)
            logging.info("k1_cfs_bus: Transport = CAN (%s) "
                         "[EXPERIMENTELL]", iface)
        else:
            raise self.printer.command_error(
                "k1_cfs_bus: unbekannter transport '%s'" % name)
        return self._backend

    # ---- einheitliche Schnittstelle fuer die CFS-Logik -------------------
    def xfer(self, addr, payload, read_len=64, timeout=0.2):
        """Frame bauen, senden, eine begrenzte Antwort zurueckgeben.
        Rueckgabe: (gesendetes_frame_bytes, antwort_bytes)."""
        backend = self._select_backend()
        return backend.xfer(addr, payload, read_len=read_len, timeout=timeout)

    def active_transport(self):
        self._select_backend()
        return 'rs485' if isinstance(self._backend, _RS485Transport) else 'can'

    def _handle_disconnect(self):
        if self._backend is not None:
            self._backend.close()
            self._backend = None


def load_config(config):
    return K1CFSBus(config)
