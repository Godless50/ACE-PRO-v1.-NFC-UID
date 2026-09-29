#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# artefacts_build_rc522_tunnel.py — reproducible builder for the ACE Pro (Gen 1)
# RC522/NFC firmware patch.
#
# Copyright (C) 2026 ACE-UID contributors
#
# What it does
# ------------
# Takes the community base image ACE_V1.3.863_20260716.bin (OpenCubic v1.0.2,
# md5 9f7b9a678a96caf98d6a08842d3ff971, 113720 B) and produces a patched image:
#
#   1. assembles a Thumb-2 stub (arm-none-eabi-as / -ld / -objcopy,
#      -mcpu=cortex-m4 -mthumb, linked with -Ttext <VA> of the image tail);
#   2. appends the stub at the end of the image (VA 0x08008000 + len(base));
#   3. overwrites 4 bytes at the hook VA with a Thumb-2 `bl` to the stub;
#   4. optionally rewrites the `CV<a>.<b>.<c>` version string;
#   5. reports md5, size, crc16 and the changed VA ranges.
#
# Reproduction contract
# ---------------------
# With `hook_fixed=True` the builder applies the *frozen production parser
# patch*: the 4 hook bytes at VA 0x08016B9A plus the existing 108-byte parser
# stub (artefacts_stub_uid_cfw.s).  Per the plan's Global Constraints that hook
# and its stub are never changed, so the builder freezes them instead of
# deriving them from an arbitrary source.  This reproduces the recorded image
# ACE_V1.3.863_cfw_uid.bin byte-for-byte:
#
#   md5    6148cfc52431fc235536c6d64b5334ef
#   size   113828
#   crc16  0xAE92   (CRC-16/MCRF4XX)
#
# Size ceiling (firmware property)
# -------------------------------
# The app start is 0x08008000 and the IAP staging base is 0x08024000, so the
# applied image must be <= 0x1C000 = 114688 bytes (MAX_IMAGE_BYTES).  The base
# is 113720 B, leaving a 968 B tail budget for the frozen 108 B parser stub plus
# the extra stub.  An image past the ceiling is accepted by `iap_upgrade`
# (code 0) but never commits (the unit keeps running the previous image), so
# `build()` raises ValueError when the output would exceed MAX_IMAGE_BYTES.
#
# When `hook_fixed=False` (default) the builder is generic: it assembles the
# stub given via `stub_src` and computes the `bl` from `hook_va` to the tail.
# That is the mode later tasks use to inject the tunnel/probe stubs.
#
# When `hook_fixed=True` *and* `hook_va` names a VA other than the parser hook,
# the frozen parser patch is applied **and** a second hook is added: the stub
# from `stub_src` is linked at the tail right after the 108-byte parser stub,
# and a `bl` to it is written at `hook_va`.  This is how the P1 probe image is
# built (parser patch + probe hook), so the image keeps the working parser and
# adds the probe.
#
# NOTE on `stub_src` with `hook_fixed=True`: the production parser stub is
# frozen, so `stub_src` is only assembled and cross-checked against it (a
# mismatch is reported in result["notes"], never silently applied).  The
# repository file traditionally passed here, artefacts_stub4.s, is the source of
# a *different* (stock-image) stub and does not match the frozen tail; see the
# module docstring of artefacts_stub_uid_cfw.s.
import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

# --- constants pinned by the plan's Global Constraints ----------------------

BASE_VA = 0x08008000
BASE_MD5 = "9f7b9a678a96caf98d6a08842d3ff971"
BASE_SIZE = 113720

# Hard image ceiling: the app starts at 0x08008000 and the IAP staging base is
# 0x08024000, so the applied image must be <= 0x1C000 = 114688 bytes.  An
# oversized image is accepted by `iap_upgrade` (code 0) but never commits, so
# this must be asserted, not eyeballed.  The base is 113720 B, leaving a 968 B
# tail budget for the frozen 108 B parser stub plus the extra stub.
MAX_IMAGE_BYTES = 0x1C000   # 114688

PARSER_HOOK_VA = 0x08016B9A
PARSER_HOOK_BYTES = bytes.fromhex("0df04df8")   # bl 0x08023C38 (file order)

DEFAULT_VERSION = "1.3.863"

HERE = os.path.dirname(os.path.abspath(__file__))
PARSER_STUB_SRC = os.path.join(HERE, "artefacts_stub_uid_cfw.s")

# --- RC522 tunnel host contract (P1; extended by Task 4) --------------------
#
# NOTE — host helpers live here on purpose: `pack_index`, `as_signed32`,
# `is_packed_index`, `is_tunnel_index`, `tunnel_probe_args`, `marker_decision`
# and `stub_branch` are the reference codec shared by the probe tool
# (`probe/probe_tunnel.py`) and the offline tests.  They mirror the Thumb stub
# branch logic and must be kept in sync with `artefacts_stub_probe.s`.  Task 4
# consumes them; no refactor before then (see docs/notes-p2-reply.md §10).
#
# The multiACE host packs a tunnel request into the single integer parameter
# `index` of the carrier command:
#   packed = 0x80000000 | (reader<<24) | (op<<16) | ((a1 & 0x3F)<<8) | (a2 & 0xFF)
# Bit 31 is the "this is a tunnel request" magic the probe stub tests; ordinary
# slot indices are small integers (< 0x20000) and never set it.
PACKED_MAGIC = 0x80000000
READ_REG_VA = 0x0800E734        # CFW read_reg(reader, reg) -> byte (thumb +1)
WRITE_REG_VA = 0x0800E7C0       # CFW write_reg(reader, reg, val) (thumb +1)
RESPOND_VA = 0x0801DC74         # CFW JSON response formatter (thumb +1)


def pack_index(op, a1=0, a2=0, reader=0):
    """Pack an RC522 tunnel request exactly like the multiACE host does."""
    return (PACKED_MAGIC | ((reader & 0x3) << 24) | ((op & 0xFF) << 16)
            | ((a1 & 0x3F) << 8) | (a2 & 0xFF))


def is_packed_index(index):
    """True when the RC522 tunnel magic (bit 31) is set in `index`."""
    return bool(index & PACKED_MAGIC)


# The handler parses `params.index` with the signed strtol core
# (0x0801863C -> 0x0801C2C6 -> _strtol_r 0x0801CEE0), whose positive overflow
# limit is LONG_MAX.  Every host value >= 0x80000000 therefore reaches the hook
# as exactly this sentinel, with the payload bits lost.
STRTOL_OVERFLOW_SENTINEL = 0x7FFFFFFF


def as_signed32(value):
    """Two's-complement view of a 32-bit value (what signed `strtol` accepts)."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & PACKED_MAGIC else value


def is_tunnel_index(index):
    """Mirrors the probe stub: bit 31 set, or the signed-strtol clamp sentinel."""
    index &= 0xFFFFFFFF
    return is_packed_index(index) or index == STRTOL_OVERFLOW_SENTINEL


def tunnel_probe_args(index):
    """(reader, a1) the P2 stub reads for `index`, or None for an ordinary one."""
    index &= 0xFFFFFFFF
    if is_packed_index(index):
        return ((index >> 24) & 0x3, (index >> 8) & 0x3F)
    if index == STRTOL_OVERFLOW_SENTINEL:
        return (0, 0x37)        # payload clamped away: the fixed P2 probe
    return None


# Reachability marker used by the diagnostic image (artefacts_stub_marker.s).
MARKER_CODE = 90                # 0x5A


def marker_decision(index):
    """Mirrors artefacts_stub_marker.s: 'value' | 'marker' | 'normal'.

    * bit 31 set            -> 'value'  (register read, answer in result.code)
    * any other index >= 4  -> 'marker' (constant 90, no hardware access)
    * index 0..3            -> 'normal' (ordinary slot, stock behaviour)
    """
    index &= 0xFFFFFFFF
    if index & PACKED_MAGIC:
        return "value"
    if index >= 4:
        return "marker"
    return "normal"


# The P1/P2 probe: op 0 = read register, reader 0, a1 = VersionReg 0x37.
# 0x80003700 == 2147497728.  (The task brief quotes 2147484976, but that decimal
# is 0x80000530 and contradicts the brief's own packing formula; use 2147497728.)
PROBE_VERSION_REG_INDEX = pack_index(0, 0x37, 0, 0)     # == 2147497728
# The same request as signed strtol accepts it (what a fixed host should send).
SIGNED_PROBE_VERSION_REG_INDEX = as_signed32(PROBE_VERSION_REG_INDEX)  # -2147469568

# Marker mode of the P3 stub (artefacts_stub_probe.s): any bit-31-clear index
# >= this value is answered with the reachability marker (code 90).  It is off
# the normal path (real indices are 0..3 or packed).
PROBE_MARKER_MIN = 0x10000


def stub_branch(index):
    """Mirrors artefacts_stub_probe.s: 'value' | 'sentinel' | 'marker' | 'normal'.

    * bit 31 set               -> 'value'    (decode reader/a1, read_reg, result.code)
    * == 0x7FFFFFFF            -> 'sentinel' (unsigned host form clamped; fixed probe)
    * >= 0x10000 (bit 31 clear)-> 'marker'   (constant 90, reachability only)
    * otherwise                -> 'normal'   (slot 0..3, stock replay)
    """
    index &= 0xFFFFFFFF
    if index & PACKED_MAGIC:
        return "value"
    if index == STRTOL_OVERFLOW_SENTINEL:
        return "sentinel"
    if index >= PROBE_MARKER_MIN:
        return "marker"
    return "normal"

AS = "arm-none-eabi-as"
LD = "arm-none-eabi-ld"
OBJCOPY = "arm-none-eabi-objcopy"


# --- helpers ---------------------------------------------------------------

def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def crc16(data: bytes) -> int:
    """CRC-16/MCRF4XX, as used by the ACE flasher."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def thumb_bl(src_va: int, dst_va: int) -> bytes:
    """Encode a Thumb-2 BL from src_va to dst_va (4 bytes, file order)."""
    offset = dst_va - (src_va + 4)
    if not -(1 << 24) <= offset < (1 << 24):
        raise ValueError("branch out of range: %#x -> %#x" % (src_va, dst_va))
    s = (offset >> 24) & 1
    i1 = (offset >> 23) & 1
    i2 = (offset >> 22) & 1
    imm10 = (offset >> 12) & 0x3FF
    imm11 = (offset >> 1) & 0x7FF
    j1 = (~(i1 ^ s)) & 1
    j2 = (~(i2 ^ s)) & 1
    return struct.pack(
        "<HH",
        0xF000 | (s << 10) | imm10,
        0xD000 | (j1 << 13) | (j2 << 11) | imm11,
    )


def _hook_hex(raw4: bytes) -> str:
    """Hex of 4 hook bytes in the repository/datasheet convention.

    The hook is a Thumb-2 instruction, so the four bytes are two little-endian
    16-bit halfwords; this renders 93 f8 36 10 as "f8931036" (the value the
    plan's Task-2 test pins).
    """
    return "".join("%04x" % struct.unpack_from("<H", raw4, i)[0] for i in (0, 2))


def _tmp_root():
    for candidate in (os.environ.get("ACE_TMP"), ".", None):
        if candidate is None or os.path.isdir(candidate):
            return candidate
    return None


def _run(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError("toolchain missing: %s" % cmd[0]) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "%s failed (%d):\n%s" % (cmd[0], exc.returncode, exc.stderr.decode("utf-8", "replace"))
        ) from exc


def assemble_stub(src_path: str, va: int) -> bytes:
    """Assemble+link+objcopy a stub source and return its raw bytes."""
    if not os.path.isfile(src_path):
        raise FileNotFoundError("stub source not found: %s" % src_path)
    tmp = tempfile.mkdtemp(prefix="rc522stub_", dir=_tmp_root())
    try:
        obj = os.path.join(tmp, "stub.o")
        elf = os.path.join(tmp, "stub.elf")
        raw = os.path.join(tmp, "stub.bin")
        _run([AS, "-mcpu=cortex-m4", "-mthumb", "-o", obj, src_path])
        _run([LD, "-Ttext", hex(va), "-o", elf, obj])
        _run([OBJCOPY, "-O", "binary", elf, raw])
        with open(raw, "rb") as handle:
            return handle.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _patch_version(img: bytearray, version: str):
    """Rewrite the CV<a>.<b>.<c> version string.  Returns (start, end) or None."""
    new = version if version.startswith("CV") else "CV" + version
    match = re.search(rb"CV\d+\.\d+\.\d+", bytes(img))
    if match is None:
        raise ValueError("version string not found in image")
    start, old = match.start(), match.group()
    end = start + len(old)
    while end < len(img) and img[end] == 0:      # grow into the trailing NUL padding
        end += 1
    available = end - start
    if len(new) >= available:                    # need room for the NUL terminator
        raise ValueError(
            "version %r (%d chars) does not fit the %d-byte field at file 0x%05X"
            % (new, len(new), available, start)
        )
    encoded = new.encode("ascii")
    img[start:start + len(encoded)] = encoded
    for i in range(start + len(encoded), end):
        img[i] = 0
    return (start, end)


def _diff_ranges(base: bytes, data: bytes):
    """Inclusive VA ranges where data differs from base."""
    shared = min(len(base), len(data))
    ranges = []
    for i in range(shared):
        if data[i] != base[i]:
            if ranges and i == ranges[-1][1] + 1:
                ranges[-1][1] = i
            else:
                ranges.append([i, i])
    if len(data) > len(base):
        if ranges and ranges[-1][1] == len(base) - 1:
            ranges[-1][1] = len(data) - 1
        else:
            ranges.append([len(base), len(data) - 1])
    elif len(base) > len(data):
        ranges.append([len(data), len(base) - 1])
    return [[lo + BASE_VA, hi + BASE_VA] for lo, hi in ranges]


# --- public API ------------------------------------------------------------

def build(base_path, out_path, *, hook_va=None, hook_bytes=None, stub_src=None,
          version=None, append=b"", hook_fixed=False):
    """Build a patched image and write it to `out_path`.

    Parameters
    ----------
    base_path : path to ACE_V1.3.863_20260716.bin (md5-checked).
    out_path  : destination file.
    hook_va   : VA of the 4-byte hook (default 0x08016B9A).  With
                `hook_fixed=True` a value other than the parser hook VA adds a
                *second* (probe) hook on top of the frozen parser patch.
    hook_bytes: optional explicit 4 bytes for the caller-specified hook; when
                omitted the `bl` from `hook_va` to the appended stub is encoded.
    stub_src  : path to the stub assembly source.
    version   : version string, with or without the "CV" prefix.
    append    : extra bytes appended after the stub (default none).
    hook_fixed: apply the frozen production parser patch (hook + stub).

    Returns a dict with md5, size, crc16, diff_ranges (inclusive VA pairs),
    hook_original (hex, halfword convention), hook_bytes, hook_va, stub_size,
    stub_va, version and notes.  When a second hook is applied the keys
    parser_hook_va, parser_hook_bytes, parser_hook_original, probe_stub_va and
    probe_stub_size are populated as well.
    """
    with open(base_path, "rb") as handle:
        base = handle.read()
    if len(base) != BASE_SIZE or md5(base) != BASE_MD5:
        raise ValueError(
            "base image mismatch: got md5=%s size=%d, expected md5=%s size=%d"
            % (md5(base), len(base), BASE_MD5, BASE_SIZE)
        )

    # `hook_fixed` freezes the parser patch at PARSER_HOOK_VA.  A different
    # `hook_va` then asks for an *additional* hook (used by the P1 probe).
    parser_fixed = bool(hook_fixed)
    extra_hook = parser_fixed and hook_va is not None and hook_va != PARSER_HOOK_VA
    if hook_va is None:
        hook_va = PARSER_HOOK_VA
    hook_off = hook_va - BASE_VA
    if hook_off < 0 or hook_off + 4 > len(base):
        raise ValueError("hook VA %#x is outside the base image" % hook_va)
    hook_original = _hook_hex(base[hook_off:hook_off + 4])

    stub_va = BASE_VA + len(base)
    notes = []
    probe_stub_va = None
    probe_stub = b""

    if parser_fixed:
        parser_stub = assemble_stub(PARSER_STUB_SRC, stub_va)
        notes.append(
            "hook_fixed: applying the frozen production parser stub "
            "(%d B from %s)" % (len(parser_stub), os.path.basename(PARSER_STUB_SRC))
        )
        if extra_hook:
            if stub_src is None:
                raise ValueError(
                    "stub_src is required to add a second hook with hook_fixed=True"
                )
            probe_stub_va = stub_va + len(parser_stub)
            probe_stub = assemble_stub(stub_src, probe_stub_va)
            notes.append(
                "hook_fixed: adding probe hook at VA %#x -> stub %r "
                "(%d B at VA %#x, after the parser stub)"
                % (hook_va, stub_src, len(probe_stub), probe_stub_va)
            )
            stub = parser_stub + probe_stub
        else:
            if stub_src is not None:
                try:
                    given = assemble_stub(stub_src, stub_va)
                except Exception as exc:  # noqa: BLE001 - report, do not fail the build
                    notes.append("stub_src=%r could not be assembled: %s" % (stub_src, exc))
                else:
                    if given != parser_stub:
                        notes.append(
                            "stub_src=%r assembles to %d bytes and does NOT match the frozen "
                            "production parser stub (%d bytes); the frozen stub is authoritative"
                            % (stub_src, len(given), len(parser_stub))
                        )
            stub = parser_stub
    else:
        if stub_src is None:
            raise ValueError("stub_src is required when hook_fixed is False")
        stub = assemble_stub(stub_src, stub_va)

    img = bytearray(base)
    img += stub
    img += append

    if parser_fixed:
        parser_bytes = thumb_bl(PARSER_HOOK_VA, stub_va)
        if parser_bytes != PARSER_HOOK_BYTES:
            raise AssertionError(
                "frozen hook bytes changed: %s != %s"
                % (parser_bytes.hex(), PARSER_HOOK_BYTES.hex())
            )
        img[PARSER_HOOK_VA - BASE_VA:PARSER_HOOK_VA - BASE_VA + 4] = parser_bytes

    if hook_bytes is not None:
        caller_bytes = bytes(hook_bytes)
        if len(caller_bytes) != 4:
            raise ValueError("hook_bytes must be exactly 4 bytes")
    else:
        target_va = probe_stub_va if extra_hook else stub_va
        caller_bytes = thumb_bl(hook_va, target_va)

    if not (parser_fixed and not extra_hook):
        # Generic mode or the additional probe hook.
        img[hook_off:hook_off + 4] = caller_bytes

    if version is not None:
        _patch_version(img, version)

    data = bytes(img)
    # Hard ceiling: the app starts at 0x08008000 and the IAP staging base is
    # 0x08024000, so anything past 0x1C000 bytes cannot be applied.  An
    # oversized image is still accepted by `iap_upgrade` (code 0) but never
    # commits, so this must be asserted, not eyeballed.
    tail_budget = MAX_IMAGE_BYTES - BASE_SIZE
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            "image too large: %d bytes > MAX_IMAGE_BYTES %d (0x%X); "
            "tail is %d bytes over the base, budget is %d (parser stub 108 B + "
            "extra stub)"
            % (len(data), MAX_IMAGE_BYTES, MAX_IMAGE_BYTES,
               len(data) - BASE_SIZE, tail_budget)
        )
    with open(out_path, "wb") as handle:
        handle.write(data)

    res = {
        "md5": md5(data),
        "size": len(data),
        "max_image_bytes": MAX_IMAGE_BYTES,
        "crc16": crc16(data),
        "diff_ranges": _diff_ranges(base, data),
        "changed_bytes": sum(
            1 for i in range(min(len(base), len(data))) if data[i] != base[i]
        ) + abs(len(data) - len(base)),
        "hook_original": hook_original,
        "hook_original_file_order": base[hook_off:hook_off + 4].hex(),
        "hook_bytes": caller_bytes.hex(),
        "hook_va": hook_va,
        "stub_size": len(stub),
        "stub_va": stub_va,
        "version": version,
        "hook_fixed": parser_fixed,
        "extra_hook": extra_hook,
        "notes": notes,
    }
    if extra_hook:
        res.update({
            "parser_hook_va": PARSER_HOOK_VA,
            "parser_hook_bytes": PARSER_HOOK_BYTES.hex(),
            "parser_hook_original": _hook_hex(
                base[PARSER_HOOK_VA - BASE_VA:PARSER_HOOK_VA - BASE_VA + 4]
            ),
            "probe_stub_va": probe_stub_va,
            "probe_stub_size": len(probe_stub),
        })
    return res


# --- CLI -------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the ACE Pro Gen 1 RC522/NFC firmware patch."
    )
    parser.add_argument("base", help="base image (ACE_V1.3.863_20260716.bin)")
    parser.add_argument("out", help="output image")
    parser.add_argument("--hook-fixed", action="store_true",
                        help="apply the frozen production parser patch (reproduces the current image)")
    parser.add_argument("--hook", type=lambda s: int(s, 0), default=None,
                        help="hook VA (generic mode, or a second hook added on top "
                             "of --hook-fixed; default 0x08016B9A)")
    parser.add_argument("--stub", default=None, help="stub assembly source")
    parser.add_argument("--version", default=None, help="version string, e.g. CV1.3.863")
    parser.add_argument("--append", default=None, help="extra bytes to append, hex")
    args = parser.parse_args(argv)

    append = bytes.fromhex(args.append) if args.append else b""
    res = build(
        args.base, args.out,
        hook_va=args.hook, stub_src=args.stub, version=args.version,
        append=append, hook_fixed=args.hook_fixed,
    )

    print("out    %s" % args.out)
    print("md5    %s" % res["md5"])
    print("size   %d" % res["size"])
    print("crc16  0x%04X" % res["crc16"])
    print("hook   0x%08X  original %s -> %s (file order %s)"
          % (res["hook_va"], res["hook_original"], res["hook_bytes"],
             res["hook_original_file_order"]))
    if res.get("extra_hook"):
        print("parser 0x%08X  original %s -> %s"
              % (res["parser_hook_va"], res["parser_hook_original"],
                 res["parser_hook_bytes"]))
        print("probe  %d B at VA 0x%08X" % (res["probe_stub_size"], res["probe_stub_va"]))
    print("stub   %d B at VA 0x%08X" % (res["stub_size"], res["stub_va"]))
    print("diff   %d B in %d range(s)" % (res["changed_bytes"], len(res["diff_ranges"])))
    for lo, hi in res["diff_ranges"]:
        print("   0x%08X..0x%08X (%d B)" % (lo, hi, hi - lo + 1))
    for note in res["notes"]:
        print("note   %s" % note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
