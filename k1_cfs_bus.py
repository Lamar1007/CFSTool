# k1_cfs_bus.py - transport layer for talking to a Creality CFS from MAINLINE
# Klipper on all K1-series printers (K1, K1C, K1 Max, K1 SE), S11 AND S12 boards.
#
# Idea: the CFS logic (ported from grant0013/K2-OpenKlipper k2_cfs.py) talks to
# ONE interface only:  frame, resp = bus.xfer(addr, payload, timeout=...)
# This file provides that interface for TWO transports. The command/frame layer
# is the same on every K1 (from the author's analysis of the stock K1 image,
# cross-checked against the K2); only the transport can differ:
#
#   RS485 : CFS via CH340 -> /dev/ttyUSB0 @ 230400     [WORKING]
#   CAN   : CFS via can0 @ 1 Mbit/s                    [EXPERIMENTAL]
#
# IMPORTANT: the transport does NOT depend on the board revision (S11/S12). The
# same S12 board can run RS485 (confirmed: a K1 Max with an S12 board talks to
# its CFS over RS485). So the transport is NOT derived from the board name but
# detected at runtime from the device that actually exists (first
# /dev/ttyUSB*/CH340, then can*). 'transport: auto' does exactly that.
#
# Shared application layer (both transports):
#   frame = 0xF7 | addr | len | payload... | crc8
#     len  = number of bytes after the len byte (payload + crc)
#     crc8 = poly 0x07, init 0x00, not reflected, xorout 0x00, over (len+payload)
#
# The RS485 path is derived from grant0013's k2_motor_bus.py (GPLv3) and matches
# the verified cfstool protocol (PROTOCOL.md).
# The CAN path is a scaffold built from evidence (cfs_iap_upgchck.sh and the
# SerialCanManager in box_wrapper.pyc) and MUST be verified with candump on an
# S12 board before it is used to move anything.
#
# Known open item before using this for [cfs] polling: xfer() has no lock. A
# timer calling xfer() while another call waits in _read_frame would flush the
# input buffer and take the other call's reply. Wrap the whole transfer in a
# reactor mutex (as k2_motor_bus.py does with _xfer_lock), and prefer a reply
# parser that checks CRC and address (like K2-OpenKlipper's proto.find_reply).
#
# License: GPLv3 (like Klipper and K2-OpenKlipper). No Creality code included;
# protocol knowledge from observation/interoperability. Credit: Grant Harkness
# (Hark Tech).
#
# Config:
#   [k1_cfs_bus]
#   #transport: auto          # auto | rs485 | can
#   #serial: /dev/ttyUSB0     # RS485: fixed path; empty => auto-detect CH340
#   #baud: 230400
#   #can_interface: can0      # CAN: interface name
#   #can_bitrate: 1000000

import glob
import logging
import os


# --------------------------------------------------------------------------
# Shared frame helpers (transport independent)
# --------------------------------------------------------------------------
def crc8(data):
    """CRC-8, poly 0x07, init 0x00, no reflection, no final XOR."""
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
    """Find the CFS's CH340 RS485 device (S11 board). Prefers the stable
    by-id path, falls back to /dev/ttyUSB*. Returns a path or None."""
    # The CFS bus itself. Some printers have a second 1a86 adapter
    # ("USB_Single_Serial") that is NOT the CFS, so match the exact name first.
    for p in sorted(glob.glob('/dev/serial/by-id/usb-1a86_USB_Serial-if00*')):
        return p
    # Stable paths (survive reboots / port changes):
    for p in sorted(glob.glob('/dev/serial/by-id/*1a86*USB*')):
        if 'Single' not in p:
            return p
    for p in sorted(glob.glob('/dev/serial/by-id/*USB_Serial*')):
        return p
    ttys = sorted(glob.glob('/dev/ttyUSB*'))
    return ttys[0] if ttys else None


def find_can_interface():
    """Find the first CAN interface (S12 board). Returns its name or None."""
    for path in sorted(glob.glob('/sys/class/net/can*')):
        return os.path.basename(path)
    return None


# --------------------------------------------------------------------------
# RS485 backend (S11)  -- working, verified against grant0013 + cfstool
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
        logging.info("k1_cfs_bus: RS485 opened %s @ %d",
                     self.serialport, self.baud)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _read_frame(self, timeout, read_len):
        # Read without blocking, pausing the reactor in between: a CFS stage can
        # take 20+ s to answer, and Klipper must keep regulating the heaters
        # meanwhile (a blocked reactor let the hotend temperature drift).
        deadline = self.reactor.monotonic() + timeout
        self.ser.timeout = 0
        buf = b''
        while True:
            buf += self.ser.read(256)
            start = buf.find(b'\xf7')          # drop noise up to the preamble
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
# CAN backend (S12)  -- EXPERIMENTAL, to be verified with candump on S12
# --------------------------------------------------------------------------
class _CANTransport:
    # Evidence (stock K1 image analysis): cfs_iap_upgchck.sh uses can0 @ 1 Mbit/s
    # and wraps the same 0xF7 frame into CAN data fields, e.g.
    #   cansend 0FD#F7FD04FF25A4E9   (enter bootloader, cutter 0xA4)
    #   cansend 0FE#F7FE04FF25A2FB   (enter bootloader, box    0xA2)
    # Those are IAP/bootloader IDs. The RUNTIME CAN IDs that box_wrapper's
    # SerialCanManager uses for normal CFS frames are NOT reversed yet -
    # so this path is a scaffold. Before moving anything: capture candump on
    # an S12 board and confirm the TX/RX CAN IDs and the fragmentation.
    TX_CAN_ID = 0x0FE   # PROVISIONAL (box). Confirm with candump!
    RX_CAN_ID = None    # to be determined with candump

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
                "CAN transport needs python-can (pip install python-can) "
                "or a socketcan fallback")
        self.bus = can.interface.Bus(channel=self.interface,
                                     bustype='socketcan')
        logging.info("k1_cfs_bus: CAN opened %s", self.interface)

    def close(self):
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None

    def xfer(self, addr, payload, read_len=64, timeout=0.2):
        # SCAFFOLD: builds the same 0xF7 frame and puts it into the CAN data
        # field. CAN frames carry at most 8 data bytes -> longer CFS frames have
        # to be fragmented (convention still to be confirmed with candump).
        self._open()
        import can
        frame = build_frame(addr, payload)
        if len(frame) > 8:
            raise NotImplementedError(
                "CAN fragmentation for frames > 8 bytes is not verified yet - "
                "a candump from an S12 board is needed")
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
# Public bus class: picks/detects the transport, one common xfer()
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

    # ---- transport selection / auto-detection ----------------------------
    def _select_backend(self):
        if self._backend is not None:
            return self._backend
        name = self.transport_name
        if name == 'auto':
            # RS485 first: is there a CH340 device? (independent of the board)
            ser = self.serialport or find_cfs_serial()
            if ser:
                name = 'rs485'
            elif find_can_interface():
                name = 'can'
            else:
                raise self.printer.command_error(
                    "k1_cfs_bus: no CFS transport found - neither "
                    "/dev/ttyUSB* (S11/RS485) nor can* (S12/CAN). Check that "
                    "the CFS is connected and powered.")
        if name == 'rs485':
            ser = self.serialport or find_cfs_serial()
            if not ser:
                raise self.printer.command_error(
                    "k1_cfs_bus: RS485 selected, but no CH340/ttyUSB "
                    "device found")
            self._backend = _RS485Transport(ser, self.baud, self.reactor)
            logging.info("k1_cfs_bus: transport = RS485 (%s)", ser)
        elif name == 'can':
            iface = self.can_interface or find_can_interface()
            if not iface:
                raise self.printer.command_error(
                    "k1_cfs_bus: CAN selected, but no can interface "
                    "found")
            self._backend = _CANTransport(iface, self.can_bitrate,
                                          self.reactor)
            logging.info("k1_cfs_bus: transport = CAN (%s) "
                         "[EXPERIMENTAL]", iface)
        else:
            raise self.printer.command_error(
                "k1_cfs_bus: unknown transport '%s'" % name)
        return self._backend

    # ---- common interface for the CFS logic ------------------------------
    def xfer(self, addr, payload, read_len=64, timeout=0.2):
        """Build a frame, send it, return a bounded reply.
        Returns: (sent_frame_bytes, reply_bytes)."""
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
