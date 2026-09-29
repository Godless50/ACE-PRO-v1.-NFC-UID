# SPDX-License-Identifier: GPL-3.0-or-later
#
# Offline tests for probe/probe_tunnel.py.
# Copyright (C) 2026 ACE-UID contributors
#
# No device and no serial I/O: the frame codec, packing, reply parsing and the
# TunnelProbe API are exercised against pure functions and a fake ACE transport.
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "probe"))

import ace_flash  # noqa: E402
import probe_tunnel as pt  # noqa: E402


class FakeAce:
    """Minimal stand-in for ace_flash.Ace (no serial)."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def rpc(self, method, params=None, timeout=4.0):
        self.calls.append((method, params, timeout))
        if method == "get_status":
            return {"id": 1, "result": {"slots": []}}
        return self.reply

    def prime(self, tries=40):
        return True

    def close(self):
        pass


# --- wire codec (reused from ace_flash) -------------------------------------

def test_frame_codec_is_the_ace_flash_one():
    payload = b'{"id":1,"method":"get_status"}'
    f = ace_flash.frame(payload)
    assert f[:2] == b"\xff\xaa"
    assert f[2:4] == len(payload).to_bytes(2, "little")
    assert f[4:4 + len(payload)] == payload
    assert f[-3:-1] == ace_flash.crc16(payload).to_bytes(2, "little")
    assert f[-1:] == b"\xfe"
    # probe_tunnel re-exports the same codec objects
    assert pt.frame is ace_flash.frame
    assert pt.crc16 is ace_flash.crc16


# --- packing (R10: signed form is canonical) --------------------------------

def test_pack_and_signed_form():
    packed = pt.pack_index(0, 0x37, 0, 0)
    assert packed == 0x80003700
    assert packed == 2147497728
    # R10: the host must send packed - 2**32
    assert pt.as_signed32(packed) == -2147469568
    assert pt.as_signed32(2) == 2
    assert pt.as_signed32(0x7FFFFFFF) == 0x7FFFFFFF


def test_pack_fields():
    assert pt.unpack_index(pt.pack_index(3, 0x2A, 0x11, 1)) == (1, 3, 0x2A, 0x11)
    assert pt.unpack_index(-2147469568) == (0, 0, 0x37, 0)
    assert pt.pack_index(0, 0x37, 0, 0) == 0x80000000 | 0x3700


# --- reply parsing ----------------------------------------------------------

def test_extract_value_reads_result_code():
    assert pt.extract_value({"id": 7, "result": {"code": 161}, "msg": "ok"}) == ("code", 161)
    assert pt.extract_value({"result": {"code": 0x1A1}}) == ("code", 0xA1)
    assert pt.extract_value({"result": {}}) == (None, None)
    assert pt.extract_value({"result": {"v": 161}}) == (None, None)
    assert pt.extract_value(None) == (None, None)


# --- TunnelProbe API --------------------------------------------------------

def test_send_builds_signed_index_and_parses_reply():
    fake = FakeAce(reply={"id": 7, "result": {"code": 161}, "msg": "ok"})
    probe = pt.TunnelProbe(ace=fake)
    reply = probe.send(0, a1=0x37)
    assert reply.field == "code"
    assert reply.value == 161
    assert reply.raw["result"]["code"] == 161
    # a keepalive preceded the first probe (unit resets after ~3.3 s idle)
    assert fake.calls[0][0] == "get_status"
    method, params, _ = [c for c in fake.calls if c[0] == "filament_recognition"][-1]
    assert method == "filament_recognition"
    assert params == {"index": -2147469568}      # R10 signed form


def test_send_packed_and_read_reg():
    fake = FakeAce(reply={"id": 1, "result": {"code": 161}})
    probe = pt.TunnelProbe(ace=fake)
    assert probe.send_packed(0x80003700).value == 161
    assert fake.calls[-1][1] == {"index": -2147469568}
    assert probe.read_reg(0x37).value == 161
    reader, op, a1, a2 = pt.unpack_index(pt.pack_index(0, 0x37))
    assert (reader, op, a1, a2) == (0, 0, 0x37, 0)
    params = fake.calls[-1][1]
    assert params["index"] == -2147469568


def test_keepalive_only_after_a_gap():
    fake = FakeAce(reply={"id": 1, "result": {"code": 161}})
    probe = pt.TunnelProbe(ace=fake)
    probe.send(0, a1=0x37)
    n_before = len([c for c in fake.calls if c[0] == "get_status"])
    probe.send(0, a1=0x37)
    n_after = len([c for c in fake.calls if c[0] == "get_status"])
    # back-to-back probes do not need another keepalive
    assert n_after == n_before


# --- CLI --------------------------------------------------------------------

def test_cli_parses_probe_and_read_reg():
    parser = pt._build_parser()
    a = parser.parse_args(["probe", "--dev", "/dev/ttyX", "--idx", "1"])
    assert a.cmd == "probe" and a.a1 == 0x37 and a.op == 0 and a.idx == 1
    a = parser.parse_args(["probe", "--dev", "/dev/ttyX", "--packed", "0x80003700"])
    assert a.packed == 0x80003700
    a = parser.parse_args(["read-reg", "--dev", "/dev/ttyX", "--reg", "0x37"])
    assert a.cmd == "read-reg" and a.reg == 0x37


def test_module_imports_without_pyserial():
    # ace_flash imports serial lazily inside Ace._open, so importing the probe
    # tool must not require pyserial.
    import importlib
    importlib.reload(pt)
    assert hasattr(pt, "TunnelProbe")
