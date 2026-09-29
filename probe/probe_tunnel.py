#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# probe/probe_tunnel.py — host-side RC522 tunnel probe for the ACE Pro (Gen 1).
#
# Copyright (C) 2026 ACE-UID contributors
#
# The wire codec is reused verbatim from `ace_flash.py`:
#   FF AA | len(u16 LE) | payload | crc16(MCRF4XX) | FE
#   payload[0] == 0x55 -> IAP chunk, otherwise a JSON-RPC request.
#
# The tunnel request is carried by the `filament_recognition` command's integer
# `index` parameter:
#
#   packed = 0x80000000 | (reader << 24) | (op << 16)
#                       | ((a1 & 0x3F) << 8) | (a2 & 0xFF)
#
# RULING R10 — the canonical host form is the SIGNED 32-bit value: the firmware
# parses the parameter with a signed `strtol` (0x0801863C -> _strtol_r
# 0x0801CEE0), so a value >= 2**31 must be sent as `packed - 2**32`
# (e.g. 0x80003700 -> -2147469568).  The unsigned form is a lossy fallback: the
# firmware clamps it to 0x7FFFFFFF, and only the fixed op0/reader0/reg0x37 probe
# survives.
#
# The unit self-resets after ~3.3 s idle, so a light get_status keepalive is
# issued before a probe that follows a gap.
import argparse
import json
import os
import sys
import time
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ace_flash import Ace, crc16, frame  # noqa: E402

PACKED_MAGIC = 0x80000000
DEFAULT_METHOD = "filament_recognition"
IDLE_RESET_S = 3.3
KEEPALIVE_MARGIN_S = 0.8

Reply = namedtuple("Reply", "field value raw")


def pack_index(op, a1=0, a2=0, reader=0):
    """Build the unsigned packed tunnel request (see the module docstring)."""
    return (PACKED_MAGIC | ((reader & 0x3) << 24) | ((op & 0xFF) << 16)
            | ((a1 & 0x3F) << 8) | (a2 & 0xFF))


def unpack_index(packed):
    """(reader, op, a1, a2) from a packed request (unsigned or signed)."""
    value = packed & 0xFFFFFFFF
    return ((value >> 24) & 0x3, (value >> 16) & 0xFF,
            (value >> 8) & 0x3F, value & 0xFF)


def as_signed32(value):
    """R10: send the packed value in the firmware's signed 32-bit form."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & PACKED_MAGIC else value


def crc_a(data, init=0x6363):
    """RC522 CRC_A: poly 0x8408 (reflected), init 0x6363, no final xor.

    With `init=0xFFFF` this is CRC-16/MCRF4XX (check value 0x6F91 for the ASCII
    string "123456789"), which the tests use to validate the bit loop.
    """
    crc = init
    for byte in bytes(data):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def extract_value(reply):
    """(field, value) from a JSON-RPC reply; the host reads result.code & 0xFF."""
    if not isinstance(reply, dict):
        return (None, None)
    result = reply.get("result")
    if isinstance(result, dict) and "code" in result:
        try:
            return ("code", int(result["code"]) & 0xFF)
        except (TypeError, ValueError):
            return ("code", None)
    return (None, None)


class TunnelProbe:
    """Drive the RC522 tunnel through a `filament_recognition` carrier.

    `dev` is the serial device path (opened via `ace_flash.Ace`); pass an
    already-constructed `ace=` object to inject a fake in tests.  `idx` is the
    ACE device number, kept for logging/parity with the shell commands.
    """

    def __init__(self, dev=None, idx=0, *, ace=None, baud=115200,
                 method=DEFAULT_METHOD, idle_reset_s=IDLE_RESET_S):
        self.dev = dev
        self.idx = idx
        self.method = method
        self.idle_reset_s = idle_reset_s
        self.ace = ace if ace is not None else Ace(dev, baud=baud)
        self.last_activity = 0.0

    # -- transport ----------------------------------------------------------

    def _touch(self, timeout=1.0):
        """Keep the link alive across the unit's ~3.3 s idle reset."""
        now = time.time()
        if self.last_activity and now - self.last_activity < self.idle_reset_s - KEEPALIVE_MARGIN_S:
            return
        self.ace.rpc("get_status", timeout=timeout)
        self.last_activity = time.time()

    def prime(self, tries=40):
        return bool(self.ace.prime(tries))

    def close(self):
        self.ace.close()

    # -- API ----------------------------------------------------------------

    def send(self, op, a1=0, a2=0, reader=0, timeout=4.0):
        """Send one packed tunnel request; return Reply(field, value, raw)."""
        packed = pack_index(op, a1, a2, reader)
        self._touch()
        raw = self.ace.rpc(self.method, {"index": as_signed32(packed)}, timeout=timeout)
        self.last_activity = time.time()
        field, value = extract_value(raw)
        return Reply(field, value, raw)

    def send_packed(self, packed, timeout=4.0):
        """Send an already-packed value (converted to the signed form per R10)."""
        self._touch()
        raw = self.ace.rpc(self.method, {"index": as_signed32(packed)}, timeout=timeout)
        self.last_activity = time.time()
        field, value = extract_value(raw)
        return Reply(field, value, raw)

    def read_reg(self, reg, reader=0, timeout=4.0):
        """op 0: read RC522 register `reg` from `reader` (VersionReg 0x37 -> 0xA1)."""
        return self.send(0, a1=reg, reader=reader, timeout=timeout)

    # -- op conveniences (see docs/notes-ops.md) ---------------------------

    def write_reg(self, reg, value, reader=0, timeout=4.0):
        """op 1: write `value` to RC522 register `reg`."""
        return self.send(1, a1=reg, a2=value, reader=reader, timeout=timeout)

    def fifo_write(self, byte, reader=0, timeout=4.0):
        """op 2: push one byte into the RC522 FIFO."""
        return self.send(2, a1=0, a2=byte, reader=reader, timeout=timeout)

    def pcd(self, tx_len, command, reader=0, timeout=4.0):
        """op 3: run PCD `command` (0x0C = TRANSCEIVE) after `tx_len` TX bytes."""
        return self.send(3, a1=tx_len, a2=command, reader=reader, timeout=timeout)

    def fifo_read(self, reader=0, timeout=4.0):
        """op 4: pop one byte from the RC522 FIFO."""
        return self.send(4, a1=0, reader=reader, timeout=timeout)

    def rx_bits(self, reader=0, timeout=4.0):
        """op 5: received bit count (0x80 for a full 16-byte frame)."""
        return self.send(5, a1=0, reader=reader, timeout=timeout)

    def select(self, reader=0, timeout=4.0):
        """op 6: reader enable + bring-up + REQA/anticollision/SELECT.

        0 = a card is in the field and ACTIVE; non-zero otherwise.
        """
        return self.send(6, reader=reader, timeout=timeout)

    # -- reader ownership (op 7 / op 8) ------------------------------------

    def acquire(self, timeout=4.0):
        """op 7: take the reader.

        Saves the stock recognition state, drives it to idle and delays so an
        in-flight NFC action finishes; every tunnel op then re-asserts idle.
        Returns Reply(field, value=saved_state, raw).  Keep the value and pass
        it to `release()`.
        """
        return self.send(7, timeout=timeout)

    def release(self, state, timeout=4.0):
        """op 8: restore the recognition state returned by `acquire()`."""
        return self.send(8, a2=int(state) & 0xFF, timeout=timeout)

    def fifo_self_test(self, byte=0x5A, reader=0, timeout=4.0):
        """Self-contained FIFO invariant: write `byte`, read it back.

        Returns (level_before, level_flushed, level_after_write, read_back,
        raw).  The write and the read are separate tunnel commands, so run this
        inside an `acquire()` hold.  read_back == byte proves the FIFO data path.
        """
        level_before = self.read_reg(0x0A, reader, timeout).value
        self.write_reg(0x0A, 0x80, reader, timeout)          # flush FIFO
        level_zero = self.read_reg(0x0A, reader, timeout).value
        self.fifo_write(byte & 0xFF, reader, timeout)
        level_after = self.read_reg(0x0A, reader, timeout).value
        read_back = self.fifo_read(reader, timeout)
        return (level_before, level_zero, level_after, read_back.value,
                read_back.raw)


# --- CLI -------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        description="RC522 tunnel probe for the ACE Pro (Gen 1) firmware.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--dev", required=True, help="serial device path on the printer")
        p.add_argument("--idx", type=int, default=0, help="ACE device number (log only)")

    p = sub.add_parser("probe", help="send a packed tunnel request")
    common(p)
    p.add_argument("--packed", type=lambda s: int(s, 0), default=None,
                   help="raw packed value (0x... or decimal); overrides op/a1/a2/reader")
    p.add_argument("--op", type=lambda s: int(s, 0), default=0)
    p.add_argument("--a1", type=lambda s: int(s, 0), default=0x37)
    p.add_argument("--a2", type=lambda s: int(s, 0), default=0)
    p.add_argument("--reader", type=lambda s: int(s, 0), default=0)
    p.add_argument("--timeout", type=float, default=4.0)

    p = sub.add_parser("read-reg", help="op 0 convenience: read an RC522 register")
    common(p)
    p.add_argument("--reg", type=lambda s: int(s, 0), required=True)
    p.add_argument("--reader", type=lambda s: int(s, 0), default=0)
    p.add_argument("--timeout", type=float, default=4.0)

    p = sub.add_parser("acquire", help="op 7: take the reader (pause stock NFC)")
    common(p)
    p.add_argument("--timeout", type=float, default=4.0)

    p = sub.add_parser("release", help="op 8: restore the stock NFC state")
    common(p)
    p.add_argument("--state", type=lambda s: int(s, 0), required=True,
                   help="the value returned by acquire")
    p.add_argument("--timeout", type=float, default=4.0)

    p = sub.add_parser("fifo-test",
                       help="op 0/1/2/4 FIFO write/read invariant (inside a hold)")
    common(p)
    p.add_argument("--byte", type=lambda s: int(s, 0), default=0x5A)
    p.add_argument("--reader", type=lambda s: int(s, 0), default=0)
    p.add_argument("--timeout", type=float, default=4.0)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    probe = TunnelProbe(args.dev, args.idx)
    try:
        if not probe.prime():
            print("probe: link not up (get_status did not answer)", file=sys.stderr)
            return 2
        if args.cmd == "read-reg":
            reply = probe.read_reg(args.reg, args.reader, timeout=args.timeout)
            print("read_reg reader=%d reg=0x%02X -> %s" % (args.reader, args.reg, reply))
        elif args.cmd == "acquire":
            reply = probe.acquire(timeout=args.timeout)
            print("acquire -> saved recognition state %s" % reply.value)
            return 0 if reply.value is not None else 1
        elif args.cmd == "release":
            reply = probe.release(args.state, timeout=args.timeout)
            print("release state=%d -> %s" % (args.state, reply))
            return 0 if reply.value == 0 else 1
        elif args.cmd == "fifo-test":
            before, flushed, after, back, raw = probe.fifo_self_test(
                args.byte, args.reader, args.timeout)
            ok = back == (args.byte & 0xFF)
            print("fifo-test: level before=%s after flush=%s after write=%s "
                  "read_back=%s raw=%s -> %s"
                  % (before, flushed, after, back, raw,
                     "OK" if ok else "FAIL"))
            return 0 if ok else 1
        else:
            if args.packed is not None:
                reply = probe.send_packed(args.packed, timeout=args.timeout)
            else:
                reply = probe.send(args.op, args.a1, args.a2, args.reader,
                                   timeout=args.timeout)
            packed = args.packed if args.packed is not None else \
                pack_index(args.op, args.a1, args.a2, args.reader)
            print("packed=0x%08X signed=%d -> %s"
                  % (packed & 0xFFFFFFFF, as_signed32(packed), reply))
        return 0 if reply.value is not None else 1
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())
