#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# probe/probe_ntag.py — STAGE-A DEBUG TOOL for reading an NTAG through the RC522
# tunnel carrier.  Copyright (C) 2026 ACE-UID contributors
#
# This is a debug host path: it drives the full RC522 sequence through the
# `filament_recognition` tunnel (ops 1/2/3/4/5/6) instead of patching multiACE.
# It is NOT the production path (that is Task 6/7's `ACE_TAG_READ`).
#
# Sequence (see docs/notes-ops.md):
#   select     : op 6 (firmware does reader enable + bring-up + REQA +
#                anticollision + SELECT)
#   read-page N: flush/clear via op 1, push the bare 0x30 <page> frame via
#                op 2, TRANSCEIVE via op 3, then op 4 pops the 16 data bytes.
#                The CRC is the RC522's own (hardware TxCRCEn, as the stock
#                firmware read does); RxCRCEn verifies and strips the reply
#                CRC, so a full reply is exactly 16 bytes (rx_bits 0x80).
#   read-full N: op 7 acquire (pause the stock NFC task), SELECT, READ, op 8
#                release -- use this one for the acceptance test
#   select-scan: one op 7 hold, op 6 SELECT on readers 0..3, op 8 release; a
#                failed select also reads ComIrqReg/ErrorReg/FIFOLevelReg so
#                the failing stage (REQA / anticollision / SELECT) is visible
#
# The tool never accesses a device from the tests; the CLI does, on the printer.
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from probe_tunnel import TunnelProbe  # noqa: E402

PAGE_READ = 0x30
PCD_TRANSCEIVE = 0x0C
R_FIFOLEVEL = 0x0A
R_COMIRQ = 0x04
R_ERROR = 0x06
R_BITFRAMING = 0x0D

# Register order for the failed-SELECT diagnosis (docs/notes-ops.md §4c):
#   ComIrqReg 0x04: 0x01 TimerIRq only -> REQA timed out (no field / no tag)
#                    0x20 RxIRq      -> a frame arrived (advance to ErrorReg)
#   ErrorReg  0x06: 0x08 CRCErr / 0x10 CollErr -> the anticollision/SELECT
#                    frame or the answer is corrupt / two tags
#   FIFOLevel 0x0A: what is left for the next stage
SELECT_STAGE_REGS = (("ComIrqReg", R_COMIRQ),
                     ("ErrorReg", R_ERROR),
                     ("FIFOLevelReg", R_FIFOLEVEL))


class NtagError(RuntimeError):
    pass


def read_frame(page):
    """The bare NTAG READ frame: 0x30, page.

    The RC522 appends the CRC_A itself (op 3 sets TxCRCEn, exactly like the
    stock firmware read 0x0800E3E0 and the multiACE host `_rc_setup_crc`);
    pushing a software CRC here would double it.
    """
    return bytes([PAGE_READ, page & 0xFF])


def select(probe, reader=0):
    """op 6: bring a card to ACTIVE.  Returns the Reply (value 0 = present)."""
    return probe.select(reader)


def read_page(probe, page, reader=0, length=16, strict=True):
    """Read `length` bytes starting at `page` (default 16 = pages N..N+3).

    Returns (data, bits, status_reply, frame).  With a full reply the FIFO
    holds exactly the 16 data bytes (RxCRCEn strips the tag CRC), so the
    `length` argument is only for deliberately short reads.
    """
    frame = read_frame(page)
    probe.write_reg(R_FIFOLEVEL, 0x80, reader)   # flush FIFO
    probe.write_reg(R_COMIRQ, 0x7F, reader)      # clear interrupts
    probe.write_reg(R_BITFRAMING, 0x00, reader)  # full bytes
    for byte in frame:
        probe.fifo_write(byte, reader)
    status = probe.pcd(len(frame), PCD_TRANSCEIVE, reader)
    if strict and status.value != 0:
        raise NtagError("TRANSCEIVE status 0x%02X" % (status.value or 0))
    bits = probe.rx_bits(reader).value
    data = bytes((probe.fifo_read(reader).value or 0) for _ in range(length))
    return data, bits, status, frame


def read_full(probe, page, reader=0, length=16, strict=False):
    """op 7 hold -> op 6 SELECT -> READ(page) -> op 8 release.

    The whole sequence runs in one process so the reader ownership hold spans
    every command.  Returns (select_reply, data, bits, status, frame, saved).
    """
    saved = probe.acquire().value
    try:
        sel = select(probe, reader)
        if sel.value != 0:
            return sel, None, None, None, None, saved
        data, bits, status, frame = read_page(
            probe, page, reader, length, strict=strict)
        return sel, data, bits, status, frame, saved
    finally:
        if saved is not None:
            probe.release(saved)


def select_scan(probe, readers=(0, 1, 2, 3), repeat=1):
    """op 7 hold -> op 6 SELECT on every reader -> op 8 release.

    One hold spans the whole sweep, so the stock recognition task cannot
    reset the chip between the per-reader SELECTs (the failure mode that made
    earlier op-6 probes return 255; see docs/notes-ops.md §7.4).  After a
    failed select the stage registers (SELECT_STAGE_REGS) are read, which lets
    the controller tell a REQA timeout (no field / no tag) from a corrupt
    anticollision/SELECT exchange.

    Returns (saved_state, rows) with rows = [(reader, [codes], [(name, value)])].
    """
    saved = probe.acquire().value
    rows = []
    try:
        for reader in readers:
            codes = [probe.select(reader).value for _ in range(max(1, repeat))]
            stages = []
            if codes[-1] != 0:
                for name, reg in SELECT_STAGE_REGS:
                    stages.append((name, probe.read_reg(reg, reader).value))
            rows.append((reader, codes, stages))
    finally:
        if saved is not None:
            probe.release(saved)
    return saved, rows


def _parser():
    p = argparse.ArgumentParser(
        description="Stage-A NTAG debug driver over the RC522 tunnel.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--dev", required=True, help="serial device path on the printer")
        sp.add_argument("--idx", type=int, default=0, help="ACE device number (log only)")
        sp.add_argument("--reader", type=lambda s: int(s, 0), default=0,
                        help="reader/antenna context 0..3")

    sp = sub.add_parser("select", help="REQA + anticollision + SELECT")
    common(sp)

    sp = sub.add_parser("read-page", help="NTAG READ(0x30) from a page")
    common(sp)
    sp.add_argument("--page", type=lambda s: int(s, 0), required=True)
    sp.add_argument("--length", type=int, default=16, help="data bytes to return")

    sp = sub.add_parser("read-full",
                        help="op7 hold + SELECT + READ(page) + op8 release")
    common(sp)
    sp.add_argument("--page", type=lambda s: int(s, 0), required=True)
    sp.add_argument("--length", type=int, default=16, help="data bytes to return")

    sp = sub.add_parser("select-scan",
                        help="op7 hold + SELECT on readers 0..3 + op8 release")
    common(sp)
    sp.add_argument("--readers", default="0,1,2,3",
                    help="comma-separated readers to sweep")
    sp.add_argument("--repeat", type=int, default=1,
                    help="SELECT attempts per reader")
    return p


def main(argv=None):
    args = _parser().parse_args(argv)
    probe = TunnelProbe(args.dev, args.idx)
    try:
        if not probe.prime():
            print("probe: link not up", file=sys.stderr)
            return 2
        if args.cmd == "select":
            reply = select(probe, args.reader)
            ok = reply.value == 0
            print("SELECT reader=%d -> code=%s (%s)"
                  % (args.reader, reply.value, "card present" if ok else "no card"))
            return 0 if ok else 1
        if args.cmd == "select-scan":
            readers = [int(x, 0) for x in str(args.readers).split(",") if x.strip()]
            saved, rows = select_scan(probe, readers, args.repeat)
            print("acquire -> saved state %s" % saved)
            any_ok = False
            for reader, codes, stages in rows:
                shown = ",".join(str(c) for c in codes)
                print("reader %d: SELECT code(s) [%s] -> %s"
                      % (reader, shown,
                         "card present" if 0 in codes else "no card"))
                any_ok = any_ok or 0 in codes
                if stages:
                    print("          stages: %s"
                          % ", ".join("%s=%s" % (n, v) for n, v in stages))
            print("release -> restored state %s" % saved)
            return 0 if any_ok else 1
        length = args.length
        if args.cmd == "read-full":
            sel, data, bits, status, frame, saved = read_full(
                probe, args.page, args.reader, length)
            print("acquire -> saved state %s; SELECT -> code=%s"
                  % (saved, sel.value))
            if data is None:
                return 1
            print("page=%d frame=%s status=%s bits=%s" % (args.page, frame.hex(),
                                                          status.value, bits))
            print("data[%d] = %s" % (len(data), data.hex()))
            return 0 if status.value == 0 else 1
        data, bits, status, frame = read_page(
            probe, args.page, args.reader, length, strict=False)
        print("page=%d frame=%s status=%s bits=%s" % (args.page, frame.hex(),
                                                      status.value, bits))
        print("data[%d] = %s" % (len(data), data.hex()))
        return 0 if status.value == 0 else 1
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())
