# SPDX-License-Identifier: GPL-3.0-or-later
#
# Offline tests for the probe hook (artefacts_stub_probe.s + builder).
# Copyright (C) 2026 ACE-UID contributors
#
# Everything here is testable without the printer: the pinned P3 image, the
# frozen parser patch staying byte-identical, the append layout, the hook
# encoding, the reply format strings and the index branch codec that mirrors the
# stub (including the P3 [sp,#12] frame fix).
import pathlib
import struct
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from artefacts_build_rc522_tunnel import (  # noqa: E402
    BASE_MD5,
    BASE_SIZE,
    BASE_VA,
    PARSER_HOOK_VA,
    PARSER_STUB_SRC,
    PROBE_MARKER_MIN,
    PROBE_VERSION_REG_INDEX,
    READ_REG_VA,
    RESPOND_VA,
    SIGNED_PROBE_VERSION_REG_INDEX,
    STRTOL_OVERFLOW_SENTINEL,
    as_signed32,
    build,
    is_packed_index,
    is_tunnel_index,
    pack_index,
    stub_branch,
    thumb_bl,
    tunnel_probe_args,
)

PROBE_HOOK_VA = 0x0801450A
PARSER_STUB_VA = BASE_VA + BASE_SIZE            # 0x08023C38
PROBE_STUB_VA = PARSER_STUB_VA + 108            # 0x08023CA4
PROBE_STUB_SIZE = 208
PROBE_STUB_SRC = "artefacts_stub_probe.s"
WORKING_MD5 = "6148cfc52431fc235536c6d64b5334ef"

# Recorded identity of the P3 (corrected) image (see docs/notes-p2-reply.md §9).
PROBE_MD5 = "a6d9230ae58ecc2370c42169e2b73f3e"
PROBE_SIZE = 114036
PROBE_CRC16 = 0xBE32
PROBE_VERSION = "1.3.865"
PROBE_VERSION_BYTE_VA = 0x08020AE8
VALUE_FMT = b'{"id":%d,"result":{"code":%d},"msg":"ok"}'
MARKER_FMT = b'{"id":%d,"result":{"code":%d},"msg":"marker"}'
TAIL_SIZE = 108 + PROBE_STUB_SIZE               # parser stub + probe stub


@pytest.fixture(autouse=True)
def _repo_root(monkeypatch):
    monkeypatch.chdir(ROOT)


def _build_probe(tmp_path):
    return build("ACE_V1.3.863_20260716.bin", str(tmp_path / "probe.bin"),
                 hook_fixed=True, hook_va=PROBE_HOOK_VA,
                 stub_src=PROBE_STUB_SRC, version=PROBE_VERSION)


# --- index codec: mirrors the stub's branch tests ---------------------------

def test_packed_index_helper_matches_host_contract():
    assert PROBE_VERSION_REG_INDEX == 2147497728
    assert PROBE_VERSION_REG_INDEX == 0x80003700
    assert pack_index(0, 0x37, 0, 0) == 0x80000000 | 0x3700


def test_signed_form_survives_strtol():
    assert SIGNED_PROBE_VERSION_REG_INDEX == -2147469568
    assert as_signed32(0x80003700) == -2147469568
    assert as_signed32(2) == 2


def test_stub_branch_matches_delivered_decisions():
    # correct host form: signed packed request -> value path
    assert stub_branch(SIGNED_PROBE_VERSION_REG_INDEX & 0xFFFFFFFF) == "value"
    assert stub_branch(PROBE_VERSION_REG_INDEX) == "value"
    # unsigned host form: clamped to the strtol sentinel -> fixed probe
    assert stub_branch(STRTOL_OVERFLOW_SENTINEL) == "sentinel"
    # marker mode (off the normal path): large non-packed index
    assert stub_branch(1_000_000) == "marker"
    assert stub_branch(PROBE_MARKER_MIN) == "marker"
    # ordinary slots -> stock
    for slot in (0, 1, 2, 3):
        assert stub_branch(slot) == "normal"
    assert stub_branch(4) == "normal"


def test_tunnel_probe_args_matches_stub_decisions():
    assert tunnel_probe_args(PROBE_VERSION_REG_INDEX) == (0, 0x37)
    assert tunnel_probe_args(pack_index(0, 0x0D, 0, 1)) == (1, 0x0D)
    assert tunnel_probe_args(STRTOL_OVERFLOW_SENTINEL) == (0, 0x37)
    for plain in (0, 1, 2, 3, 0x20000):
        assert tunnel_probe_args(plain) is None


def test_is_tunnel_index_bit31_or_sentinel():
    assert is_tunnel_index(PROBE_VERSION_REG_INDEX)
    assert is_tunnel_index(STRTOL_OVERFLOW_SENTINEL)
    for plain in (0, 1, 2, 3, 0x20000, 0x7FFFFFFE):
        assert not is_tunnel_index(plain)
    assert is_packed_index(PROBE_VERSION_REG_INDEX)
    assert not is_packed_index(STRTOL_OVERFLOW_SENTINEL)


# --- the frozen parser patch is preserved ----------------------------------

def test_probe_keeps_frozen_parser_patch(tmp_path):
    res = _build_probe(tmp_path)
    data = (tmp_path / "probe.bin").read_bytes()
    working = (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()

    assert res["parser_hook_va"] == PARSER_HOOK_VA
    assert res["parser_hook_original"] == "f8931036"
    assert res["parser_hook_bytes"] == "0df04df8"
    hook_off = PARSER_HOOK_VA - BASE_VA
    assert data[hook_off:hook_off + 4] == working[hook_off:hook_off + 4]
    assert data[PARSER_STUB_VA - BASE_VA:PARSER_STUB_VA - BASE_VA + 108] \
        == working[BASE_SIZE:]


# --- the probe hook and stub ------------------------------------------------

def test_probe_hook_original_bytes(tmp_path):
    res = _build_probe(tmp_path)
    assert res["hook_va"] == PROBE_HOOK_VA
    assert res["hook_original"] == "f009fbb3"
    assert res["hook_original_file_order"] == "09f0b3fb"


def test_probe_hook_is_a_bl_to_the_probe_stub(tmp_path):
    res = _build_probe(tmp_path)
    data = (tmp_path / "probe.bin").read_bytes()
    hook_off = PROBE_HOOK_VA - BASE_VA
    assert data[hook_off:hook_off + 4] == thumb_bl(PROBE_HOOK_VA, PROBE_STUB_VA)
    assert res["hook_bytes"] == thumb_bl(PROBE_HOOK_VA, PROBE_STUB_VA).hex()
    assert res["probe_stub_va"] == PROBE_STUB_VA
    assert res["probe_stub_size"] == PROBE_STUB_SIZE


def test_probe_stub_reads_the_handler_sp12_frame(tmp_path):
    """P3 fix: `bl` does not push, so the index is at [sp,#12], not [sp,#16]."""
    _build_probe(tmp_path)
    data = (tmp_path / "probe.bin").read_bytes()
    stub = data[PROBE_STUB_VA - BASE_VA:PROBE_STUB_VA - BASE_VA + PROBE_STUB_SIZE]

    # ldr.w ip, [sp, #12]
    assert stub.startswith(bytes.fromhex("ddf80cc0"))
    # the bad read must be gone
    assert not stub.startswith(bytes.fromhex("ddf810c0"))


def test_probe_stub_uses_cfw_primitives_and_formats(tmp_path):
    _build_probe(tmp_path)
    data = (tmp_path / "probe.bin").read_bytes()
    stub = data[PROBE_STUB_VA - BASE_VA:PROBE_STUB_VA - BASE_VA + PROBE_STUB_SIZE]

    assert struct.pack("<I", READ_REG_VA | 1) in stub
    assert struct.pack("<I", RESPOND_VA | 1) in stub
    assert struct.pack("<I", STRTOL_OVERFLOW_SENTINEL) in stub
    assert struct.pack("<I", PROBE_MARKER_MIN) in stub
    assert VALUE_FMT + b"\x00" in stub
    assert MARKER_FMT + b"\x00" in stub
    # value replies carry result.code, not a `v` field
    assert b'"v"' not in stub


# --- the probe image as a whole --------------------------------------------

def test_probe_image_identity(tmp_path):
    res = _build_probe(tmp_path)
    assert res["md5"] == PROBE_MD5
    assert res["size"] == PROBE_SIZE
    assert res["crc16"] == PROBE_CRC16


def test_probe_changes_are_hook_version_and_appended_stub(tmp_path):
    res = _build_probe(tmp_path)
    assert res["changed_bytes"] == 2 + 4 + 1 + TAIL_SIZE
    assert res["diff_ranges"] == [
        [PROBE_HOOK_VA, PROBE_HOOK_VA],
        [PROBE_HOOK_VA + 2, PROBE_HOOK_VA + 2],
        [PARSER_HOOK_VA, PARSER_HOOK_VA + 3],
        [PROBE_VERSION_BYTE_VA, PROBE_VERSION_BYTE_VA],
        [PARSER_STUB_VA, PARSER_STUB_VA + TAIL_SIZE - 1],
    ]
    assert res["stub_size"] == TAIL_SIZE


def test_probe_version_is_distinctive(tmp_path):
    _build_probe(tmp_path)
    data = (tmp_path / "probe.bin").read_bytes()
    base = (ROOT / "ACE_V1.3.863_20260716.bin").read_bytes()
    assert b"CV1.3.865\x00" in data
    assert base[PROBE_VERSION_BYTE_VA - BASE_VA] == ord("3")
    assert data[PROBE_VERSION_BYTE_VA - BASE_VA] == ord("5")


def test_probe_image_equals_working_plus_delta(tmp_path):
    _build_probe(tmp_path)
    probe = (tmp_path / "probe.bin").read_bytes()
    working = (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()
    changed = [i for i in range(len(probe))
               if probe[i:i + 1] != working[i:i + 1]]
    assert len(changed) == 2 + 1 + PROBE_STUB_SIZE


def test_appended_tail_is_within_budget(tmp_path):
    res = _build_probe(tmp_path)
    assert res["probe_stub_size"] <= 968


def test_builder_still_reproduces_working_image(tmp_path):
    out = tmp_path / "working.bin"
    res = build("ACE_V1.3.863_20260716.bin", str(out), hook_fixed=True,
                version="1.3.863")
    assert res["md5"] == WORKING_MD5
    assert out.read_bytes() == (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()
    assert PARSER_STUB_SRC.endswith("artefacts_stub_uid_cfw.s")


def test_base_md5_guard_still_applies(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\x00" * 16)
    with pytest.raises(ValueError):
        build(str(bad), str(tmp_path / "o.bin"), hook_fixed=True)
