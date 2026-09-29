# SPDX-License-Identifier: GPL-3.0-or-later
#
# Offline tests for the merged Task 4+5 deliverables:
#   artefacts_stub_rc522.s (ops 0..8), the ACE_V1.3.863_tunnel_ops.bin image,
#   probe/probe_tunnel.py op helpers, probe/probe_ntag.py sequence.
# Copyright (C) 2026 ACE-UID contributors
#
# No device / no serial I/O: pure functions, a fake ACE transport and a fake
# tunnel probe.
import pathlib
import struct
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "probe"))

import ace_flash  # noqa: E402
import probe_ntag as nt  # noqa: E402
import probe_tunnel as pt  # noqa: E402
from artefacts_build_rc522_tunnel import (  # noqa: E402
    BASE_SIZE,
    BASE_VA,
    MAX_IMAGE_BYTES,
    PARSER_HOOK_VA,
    PROBE_VERSION_REG_INDEX,
    READ_REG_VA,
    RESPOND_VA,
    STRTOL_OVERFLOW_SENTINEL,
    WRITE_REG_VA,
    build,
    stub_branch,
)

# Fix round 2 moved the hook EARLY, from the reply convergence point
# 0x0801450A to the handler's first call 0x080144F2 (`bl 0x08011D74`), so a
# tunnel index never reaches the stock recognition kick (set_active_slot /
# state=2) before our reply.
OPS_HOOK_VA = 0x080144F2
PARSER_STUB_VA = BASE_VA + BASE_SIZE            # 0x08023C38
OPS_STUB_VA = PARSER_STUB_VA + 108              # 0x08023CA4
OPS_STUB_SIZE = 804
OPS_STUB_SRC = "artefacts_stub_rc522.s"
OPS_TAIL = 108 + OPS_STUB_SIZE                  # 912
OPS_VERSION = "1.3.871"
VERSION_BYTE_VA = 0x08020AE8

# Fix round 6: every reply path (success, 255 failures, unknown op, marker)
# restores the recognition state captured at entry, the antenna write uses the
# firmware's read-or-write semantics (`set_bits`, 0x14 |= 3) and a failed op-6
# select retries through the firmware's full mode-1 bring-up.  The stub is
# 804 B (was 824 B), tail 912 B -- 56 B under the image ceiling.
OPS_MD5 = "219df3df77f7c7e1e15a79d580a2379e"
OPS_SIZE = 114632
OPS_CRC16 = 0x1AC9
VALUE_FMT = b'{"id":%d,"result":{"code":%d},"msg":"ok"}'

# Firmware addresses the stub must carry (see artefacts_stub_rc522.s).
REPLAY_VA = 0x08011D74          # the displaced `bl` on the ordinary path
EPILOGUE_VA = 0x0801450E        # handler epilogue (add sp,#0x14; pop ...)
BRINGUP_VA = 0x0800E314         # reader REQA + anticollision + SELECT
SET_BITS_VA = 0x0800E8E0
TIMER_VA = 0x0800E974
ENABLE_VA = 0x0800E228          # per-reader enable GPIO
INIT_ALL_VA = 0x0800F030        # all-reader GPIO/antenna init
RESET_VA = 0x0800E948           # soft reset (CommandReg = 0x0F)
CONFIG_VA = 0x0800E9EC          # component config (mode 0)
STATE_GET_VA = 0x08011E98       # recognition state byte 0x200071EC
STATE_SET_VA = 0x08011EA4
DELAY_VA = 0x0800BF1C           # osDelay(ms)


@pytest.fixture(autouse=True)
def _repo_root(monkeypatch):
    monkeypatch.chdir(ROOT)


def _build_ops(tmp_path):
    return build("ACE_V1.3.863_20260716.bin", str(tmp_path / "ops.bin"),
                 hook_fixed=True, hook_va=OPS_HOOK_VA,
                 stub_src=OPS_STUB_SRC, version=OPS_VERSION)


# --- CRC_A ----------------------------------------------------------------

def test_crc_a_matches_mcrf4xx_loop():
    # With init 0xFFFF the same bit loop is CRC-16/MCRF4XX (check 0x6F91).
    assert pt.crc_a(b"123456789", init=0xFFFF) == 0x6F91
    assert pt.crc_a(b"123456789", init=0xFFFF) == ace_flash.crc16(b"123456789")
    # RC522 init is 0x6363 (empty input returns init).
    assert pt.crc_a(b"") == 0x6363
    for data in (b"\x00", b"\xff\xff", b"OPENSPOOL"):
        assert pt.crc_a(data, init=0xFFFF) == ace_flash.crc16(data)


def test_ntag_read_frame():
    # the host frame is bare: the RC522 appends the CRC (TxCRCEn, as the
    # stock firmware read 0x0800E3E0 and the multiACE host _rc_setup_crc)
    assert nt.read_frame(0) == bytes([0x30, 0x00])
    assert nt.read_frame(0x0A) == bytes([0x30, 0x0A])
    assert len(nt.read_frame(0xFF)) == 2


# --- op packing / arg decoding ---------------------------------------------

def test_op_pack_unpack_roundtrip():
    for op in range(7):
        for reader in (0, 1):
            packed = pt.pack_index(op, a1=0x37, a2=0x5A, reader=reader)
            assert pt.unpack_index(packed) == (reader, op, 0x37, 0x5A)
            # bit 31 magic and signed form per R10
            assert packed >> 31 == 1
            assert pt.as_signed32(packed) == packed - 2 ** 32


def test_stub_branch_and_sentinel_fallback():
    # packed ops go to the dispatch; ordinary slots replay
    for op in range(7):
        assert stub_branch(pt.pack_index(op, reader=1)) == "value"
    assert stub_branch(STRTOL_OVERFLOW_SENTINEL) == "sentinel"
    for slot in (0, 1, 2, 3):
        assert stub_branch(slot) == "normal"
    assert stub_branch(1_000_000) == "marker"


# --- TunnelProbe op helpers ------------------------------------------------

class FakeAce:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {"id": 1, "result": {"code": 0}, "msg": "ok"}

    def rpc(self, method, params=None, timeout=4.0):
        self.calls.append((method, params, timeout))
        return self.reply

    def prime(self, tries=40):
        return True

    def close(self):
        pass


def test_send_builds_the_expected_index_for_each_op():
    expected = {
        0: pt.pack_index(0, a1=0x37, a2=0, reader=0),
        1: pt.pack_index(1, a1=0x0D, a2=0x00, reader=0),
        2: pt.pack_index(2, a1=0, a2=0x93, reader=0),
        3: pt.pack_index(3, a1=4, a2=0x0C, reader=0),
        4: pt.pack_index(4, a1=0, a2=0, reader=0),
        5: pt.pack_index(5, a1=0, a2=0, reader=0),
        6: pt.pack_index(6, a1=0, a2=0, reader=0),
    }
    calls = {
        0: lambda p: p.read_reg(0x37),
        1: lambda p: p.write_reg(0x0D, 0x00),
        2: lambda p: p.fifo_write(0x93),
        3: lambda p: p.pcd(4, 0x0C),
        4: lambda p: p.fifo_read(),
        5: lambda p: p.rx_bits(),
        6: lambda p: p.select(),
    }
    for op in range(7):
        fake = FakeAce()
        probe = pt.TunnelProbe(ace=fake)
        calls[op](probe)
        sent = [c for c in fake.calls if c[0] == "filament_recognition"][-1]
        assert sent[1]["index"] == pt.as_signed32(expected[op])
        assert sent[1]["index"] < 0 if expected[op] >> 31 else True


def test_acquire_release_and_fifo_self_test_build_the_expected_index():
    # op 7: acquire, no a1/a2
    fake = FakeAce()
    probe = pt.TunnelProbe(ace=fake)
    probe.acquire()
    sent = [c for c in fake.calls if c[0] == "filament_recognition"][-1]
    assert pt.unpack_index(sent[1]["index"]) == (0, 7, 0, 0)

    # op 8: release with the saved state in a2
    fake = FakeAce()
    probe = pt.TunnelProbe(ace=fake)
    probe.release(8)
    sent = [c for c in fake.calls if c[0] == "filament_recognition"][-1]
    assert pt.unpack_index(sent[1]["index"]) == (0, 8, 0, 8)

    # fifo_self_test drives op0 (level) / op1 (flush) / op2 (write) / op0 / op4
    fake = FakeAce()
    probe = pt.TunnelProbe(ace=fake)
    before, flushed, after, back, raw = probe.fifo_self_test(0x5A)
    ops = [pt.unpack_index(c[1]["index"])[1]
           for c in fake.calls if c[0] == "filament_recognition"]
    assert ops == [0, 1, 0, 2, 0, 4]
    assert back == 0            # FakeAce replies code 0
    assert before == flushed == after == 0


def test_read_page_sequence_uses_ops_1_2_3_4_5():
    class FakeProbe:
        def __init__(self):
            self.calls = []
            self.out = list(range(0xA0, 0xB0))     # 16 FIFO data bytes

        def write_reg(self, reg, value, reader=0):
            self.calls.append(("w", reg, value, reader)); return pt.Reply("code", 0, None)

        def fifo_write(self, byte, reader=0):
            self.calls.append(("fw", byte, reader)); return pt.Reply("code", 0, None)

        def pcd(self, tx_len, command, reader=0):
            self.calls.append(("pcd", tx_len, command, reader)); return pt.Reply("code", 0, None)

        def rx_bits(self, reader=0):
            self.calls.append(("bits", reader)); return pt.Reply("code", 0x80, None)

        def fifo_read(self, reader=0):
            self.calls.append(("fr", reader)); return pt.Reply("code", self.out.pop(0), None)

    fake = FakeProbe()
    data, bits, status, frame = nt.read_page(fake, 0, reader=0, length=16)
    assert frame == nt.read_frame(0)
    assert fake.calls[0] == ("w", 0x0A, 0x80, 0)      # flush FIFO
    assert ("w", 0x04, 0x7F, 0) in fake.calls         # clear IRQs
    assert ("w", 0x0D, 0x00, 0) in fake.calls         # full bytes
    fw = [c[1] for c in fake.calls if c[0] == "fw"]
    assert fw == list(frame)                          # just 0x30 page
    assert ("pcd", len(frame), 0x0C, 0) in fake.calls
    assert ("bits", 0) in fake.calls
    assert data == bytes(range(0xA0, 0xB0))
    assert bits == 0x80


def test_select_uses_op6():
    calls = []

    class FakeProbe:
        def select(self, reader=0):
            calls.append(reader); return pt.Reply("code", 0, None)

    assert nt.select(FakeProbe(), reader=1).value == 0
    assert calls == [1]


def test_select_scan_sweeps_readers_under_one_hold():
    """select-scan: acquire once, op 6 per reader, stage reads only on failure,
    release once -- the required per-reader acceptance sequence."""
    class FakeProbe:
        def __init__(self):
            self.calls = []

        def acquire(self):
            self.calls.append(("acquire",)); return pt.Reply("code", 8, None)

        def release(self, state):
            self.calls.append(("release", state)); return pt.Reply("code", 0, None)

        def select(self, reader=0):
            self.calls.append(("select", reader))
            return pt.Reply("code", 0 if reader == 2 else 255, None)

        def read_reg(self, reg, reader=0):
            self.calls.append(("r", reg, reader))
            return pt.Reply("code", {0x04: 0x01, 0x06: 0x00, 0x0A: 0x00}[reg], None)

    fake = FakeProbe()
    saved, rows = nt.select_scan(fake, readers=(0, 1, 2, 3))
    assert saved == 8
    assert [r[0] for r in rows] == [0, 1, 2, 3]
    assert [r[1] for r in rows] == [[255], [255], [0], [255]]
    # only the failed readers get the stage registers
    assert rows[0][2] == [("ComIrqReg", 0x01), ("ErrorReg", 0x00),
                          ("FIFOLevelReg", 0x00)]
    assert rows[2][2] == []
    assert fake.calls[0] == ("acquire",)
    assert fake.calls[-1] == ("release", 8)
    # every select happened between the acquire and the release
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("select") == 4
    assert kinds.index("release") > max(
        i for i, k in enumerate(kinds) if k == "select")


def test_read_full_holds_the_reader_across_select_and_read():
    class FakeProbe:
        def __init__(self):
            self.calls = []

        def acquire(self):
            self.calls.append(("acquire",)); return pt.Reply("code", 8, None)

        def release(self, state):
            self.calls.append(("release", state)); return pt.Reply("code", 0, None)

        def select(self, reader=0):
            self.calls.append(("select", reader)); return pt.Reply("code", 0, None)

        def write_reg(self, reg, value, reader=0):
            self.calls.append(("w", reg, value)); return pt.Reply("code", 0, None)

        def fifo_write(self, byte, reader=0):
            self.calls.append(("fw", byte)); return pt.Reply("code", 0, None)

        def pcd(self, tx_len, command, reader=0):
            self.calls.append(("pcd", command)); return pt.Reply("code", 0, None)

        def rx_bits(self, reader=0):
            self.calls.append(("bits",)); return pt.Reply("code", 0x80, None)

        def fifo_read(self, reader=0):
            return pt.Reply("code", 0x00, None)

    fake = FakeProbe()
    sel, data, bits, status, frame, saved = nt.read_full(fake, 0, reader=0)
    assert saved == 8
    assert fake.calls[0] == ("acquire",)
    assert fake.calls[1] == ("select", 0)
    assert fake.calls[-1] == ("release", 8)
    assert bits == 0x80 and sel.value == 0
    assert len(data) == 16


# --- the ops image ----------------------------------------------------------

def test_ops_image_identity(tmp_path):
    res = _build_ops(tmp_path)
    assert res["md5"] == OPS_MD5
    assert res["size"] == OPS_SIZE
    assert res["crc16"] == OPS_CRC16
    assert res["probe_stub_size"] == OPS_STUB_SIZE
    assert res["probe_stub_va"] == OPS_STUB_VA


def test_ops_image_layout_and_version(tmp_path):
    res = _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    # the patch digit change: 1.3.863 -> 1.3.870 rewrites 0x08020AE7..AE8
    assert res["changed_bytes"] == 4 + 4 + 2 + OPS_TAIL
    assert res["diff_ranges"] == [
        [OPS_HOOK_VA, OPS_HOOK_VA + 3],
        [PARSER_HOOK_VA, PARSER_HOOK_VA + 3],
        [VERSION_BYTE_VA - 1, VERSION_BYTE_VA],
        [PARSER_STUB_VA, PARSER_STUB_VA + OPS_TAIL - 1],
    ]
    assert b"CV1.3.871\x00" in data
    assert data[VERSION_BYTE_VA - BASE_VA - 1:VERSION_BYTE_VA - BASE_VA + 1] \
        == b"71"


def test_hook_is_early_and_branches_to_the_stub(tmp_path):
    """The hook must be the handler's first call, not the reply site."""
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
    res = _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    assert res["hook_va"] == OPS_HOOK_VA == 0x080144F2
    assert res["hook_original_file_order"] == "fdf73ffc"     # bl 0x08011D74
    off = OPS_HOOK_VA - BASE_VA
    (ins,) = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN).disasm(
        data[off:off + 4], OPS_HOOK_VA)
    assert ins.mnemonic == "bl"
    assert int(ins.op_str[1:], 16) == OPS_STUB_VA


def test_ops_stub_structure(tmp_path):
    _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    stub = data[OPS_STUB_VA - BASE_VA:OPS_STUB_VA - BASE_VA + OPS_STUB_SIZE]

    # entry prologue saves 10 registers, then loads params.index at [sp,#52]
    assert stub.startswith(bytes.fromhex("2de9f84f"))   # push {r3, r4-r11, lr}
    assert bytes.fromhex("ddf834c0") in stub           # ldr.w ip, [sp, #52]
    # firmware primitives and formats
    for va in (READ_REG_VA, WRITE_REG_VA, RESPOND_VA, REPLAY_VA, EPILOGUE_VA,
               BRINGUP_VA, SET_BITS_VA, TIMER_VA,
               ENABLE_VA, INIT_ALL_VA, RESET_VA, CONFIG_VA,
               STATE_GET_VA, STATE_SET_VA, DELAY_VA):
        assert struct.pack("<I", va | 1) in stub, "missing %#x" % va
    # clear_bits is no longer used (round 6): the antenna write is set_bits
    assert struct.pack("<I", 0x0800E900 | 1) not in stub
    assert VALUE_FMT + b"\x00" in stub
    # the marker branch loads the constant 90 (0x5A) into r9
    assert bytes.fromhex("5ff05a09") in stub           # movs.w r9, #90
    # op 1 dispatches the bring-up on a TxModeReg (0x12) write
    assert bytes.fromhex("122f") in stub               # cmp r7, #0x12
    # op6 delegates to the firmware reader bring-up + SELECT (0x0800E314):
    # no hand-rolled REQA/anticollision frame or software CRC remains
    assert bytes.fromhex("2623") not in stub           # movs r3, #0x26 (REQA)


def test_ops_stub_early_exit_and_ordinary_replay(tmp_path):
    """The fix: tunnel indices return via the handler epilogue; slots replay."""
    _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    stub = data[OPS_STUB_VA - BASE_VA:OPS_STUB_VA - BASE_VA + OPS_STUB_SIZE]
    # early exit jumps to the handler epilogue 0x0801450E, so the stock
    # set_active_slot / state=2 kick (0x08014512..0x08014528) never runs
    assert struct.pack("<I", EPILOGUE_VA | 1) in stub
    # and the ordinary path tail-calls the displaced bl 0x08011D74
    assert struct.pack("<I", REPLAY_VA | 1) in stub


def test_op3_mirrors_the_firmware_read_setup(tmp_path):
    """op3 clears TxCRCEn, sets RxCRCEn and programs the 10 ms timeout."""
    _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    stub = data[OPS_STUB_VA - BASE_VA:OPS_STUB_VA - BASE_VA + OPS_STUB_SIZE]
    # clear_bits(reader, 0x12, 0x80)  -> TxMode, host frame already has its CRC
    assert bytes.fromhex("12218022") in stub
    # set_bits(reader, 0x13, 0x80)    -> RxMode, check/strip the tag CRC
    assert bytes.fromhex("13218022") in stub
    # timer_setup(reader, 10)
    assert bytes.fromhex("0a21") in stub


def test_ops_stub_uses_the_shared_reply_and_xcv_core(tmp_path):
    """The shrink kept one reply format and one bounded transceive core."""
    _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    stub = data[OPS_STUB_VA - BASE_VA:OPS_STUB_VA - BASE_VA + OPS_STUB_SIZE]
    # exactly one JSON format string, no second ("marker") variant
    assert stub.count(b'{"id":%d') == 1
    # the bounded ComIrq wait is now the firmware transceive's own 0xBB8
    assert bytes.fromhex("40f6b835") in stub           # movw r5, #0xbb8


def test_ops_stub_within_tail_budget(tmp_path):
    res = _build_ops(tmp_path)
    assert res["probe_stub_size"] <= 968


def test_builder_refuses_an_image_over_the_ceiling(tmp_path):
    assert MAX_IMAGE_BYTES == 0x1C000 == 114688
    with pytest.raises(ValueError, match="MAX_IMAGE_BYTES"):
        build("ACE_V1.3.863_20260716.bin", str(tmp_path / "big.bin"),
              hook_fixed=True, append=b"\x00" * 2000)
    # the delivered image is under the ceiling with real headroom
    # (round 5 spent 48 B on the hardware-CRC fix and the ordered bring-up;
    # 76 B are left, and the builder still refuses anything over the ceiling)
    res = _build_ops(tmp_path)
    assert res["size"] <= MAX_IMAGE_BYTES
    assert res["max_image_bytes"] == MAX_IMAGE_BYTES
    assert MAX_IMAGE_BYTES - res["size"] >= 32


def test_ops_keeps_frozen_parser_patch(tmp_path):
    _build_ops(tmp_path)
    data = (tmp_path / "ops.bin").read_bytes()
    working = (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()
    assert data[PARSER_HOOK_VA - BASE_VA:PARSER_HOOK_VA - BASE_VA + 4] \
        == working[PARSER_HOOK_VA - BASE_VA:PARSER_HOOK_VA - BASE_VA + 4]
    assert data[PARSER_STUB_VA - BASE_VA:PARSER_STUB_VA - BASE_VA + 108] \
        == working[BASE_SIZE:]


def test_builder_still_reproduces_working_image(tmp_path):
    out = tmp_path / "working.bin"
    res = build("ACE_V1.3.863_20260716.bin", str(out), hook_fixed=True,
                version="1.3.863")
    assert res["md5"] == "6148cfc52431fc235536c6d64b5334ef"
    assert out.read_bytes() == (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()


def test_ordinary_index_is_normal_not_marker_or_value():
    # bits the host never sets; 0..3 must replay the stock call
    for slot in (0, 1, 2, 3):
        assert stub_branch(slot) == "normal"
    # the canonical signed VersionReg probe is a value request
    assert stub_branch(PROBE_VERSION_REG_INDEX) == "value"


# --- stub control-flow emulation -------------------------------------------
#
# The fix round moved the hook and changed how tunnel vs ordinary indices leave
# the stub.  That decision is pure control flow, so it is exercised offline with
# a tiny Thumb interpreter over the assembled stub: a tunnel index must reply
# and leave through the handler epilogue 0x0801450E (never touching the stock
# state/slot kick), while an ordinary index must tail-call 0x08011D74 and return
# to 0x080144F7.

HELPER_NAMES = {
    READ_REG_VA: "read_reg",
    WRITE_REG_VA: "write_reg",
    SET_BITS_VA: "set_bits",
    TIMER_VA: "timer",
    ENABLE_VA: "enable",
    BRINGUP_VA: "bringup",
    INIT_ALL_VA: "init_all",
    RESET_VA: "reset",
    CONFIG_VA: "config",
    STATE_GET_VA: "state_get",
    STATE_SET_VA: "state_set",
    DELAY_VA: "delay",
    RESPOND_VA: "respond",
    REPLAY_VA: "replay",
    EPILOGUE_VA: "epilogue",
}
HELPER_RET = {"read_reg": 0xA1, "bringup": 0, "write_reg": 1, "set_bits": 1,
              "clear_bits": 1, "timer": 1, "enable": 0, "init_all": 0,
              "reset": 0, "config": 0, "state_get": 0, "state_set": 0,
              "delay": 0}


class _ThumbStub:
    """Enough of a Thumb interpreter to run the stub's decision/dispatch path."""

    def __init__(self, stub, va, hook_return=0x080144F7):
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
        self.md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
        self.md.detail = True
        self.stub = stub
        self.base = va
        self.stack_va = 0x20008000
        self.mem = bytearray(stub)
        self.reg = {n: 0 for n in
                    ("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8",
                     "r9", "r10", "r11", "r12", "sp", "lr", "pc")}
        self.reg["sp"] = self.stack_va + 0x800
        self.reg["lr"] = hook_return
        self.reg["pc"] = va
        self.calls = []          # (helper name, r0, r1, r2, r3)
        self.reply = None        # (r0, r1, r2, r3) at the formatter call
        self.exit = None         # ('handler', pc) | ('epilogue', pc)
        self.helper_ret = dict(HELPER_RET)
        self.state_writes = []   # every value passed to state_set

    # -- memory -------------------------------------------------------------
    def _word(self, va):
        if self.stack is not None and self.stack_va <= va < self.stack_va + len(self.stack):
            return self._r32(va)
        return struct.unpack_from("<I", self.mem, va - self.base)[0]

    def _w32(self, addr, value):
        self.stack[addr - self.stack_va] = value & 0xFF
        for i in range(1, 4):
            self.stack[addr - self.stack_va + i] = (value >> (8 * i)) & 0xFF

    def _r32(self, addr):
        return int.from_bytes(self.stack[addr - self.stack_va:
                                           addr - self.stack_va + 4], "little")

    # -- helpers ------------------------------------------------------------
    def _call_helper(self, va, lr):
        name = HELPER_NAMES[va]
        if name == "replay":
            self.exit = ("handler", lr)
            return False
        if name == "epilogue":
            self.exit = ("epilogue", va)
            return False
        self.calls.append((name, self.reg["r0"], self.reg["r1"],
                           self.reg["r2"], self.reg["r3"]))
        if name == "respond":
            self.reply = (self.reg["r0"], self.reg["r1"],
                          self.reg["r2"], self.reg["r3"])
            self.reg["pc"] = lr & ~1
            return True
        if name == "state_set":
            self.state_writes.append(self.reg["r0"] & 0xFF)
        self.reg["r0"] = self.helper_ret[name]
        self.reg["pc"] = lr & ~1
        return True

    # -- execution ----------------------------------------------------------
    ALIAS = {"sb": "r9", "sl": "r10", "fp": "r11", "ip": "r12"}

    def nm(self, name):
        return self.ALIAS.get(name, name)

    def run(self, index, limit=400):
        # Hook state: sp = handler local base H; params.index at H+12; the
        # handler's response buffer is in r4 and the request id in r6.
        self.reg["r4"] = 0xAA000000
        self.reg["r6"] = 0x1234
        self._w32(self.reg["sp"] + 12, index)

        for _ in range(limit):
            pc = self.reg["pc"]
            if not (self.base <= pc < self.base + len(self.stub)):
                self.exit = ("out", pc)
                return self
            ins = next(iter(self.md.disasm(
                bytes(self.mem[pc - self.base:pc - self.base + 4]), pc)))
            o = ins.operands
            m = ins.mnemonic
            rn = self.md.reg_name

            def g(name):
                return self.reg[self.nm(name)]

            def s(name, v):
                self.reg[self.nm(name)] = v & 0xFFFFFFFF

            def val(op):
                if op.type == 2:                     # CS_OP_IMM
                    return op.imm
                return g(rn(op.reg))

            def setflags(res, width=32):
                self.reg["_z"] = 1 if (res & ((1 << width) - 1)) == 0 else 0
                self.reg["_n"] = (res >> (width - 1)) & 1

            def srcs(o):
                if len(o) >= 3:
                    return val(o[1]), val(o[2])
                return (val(o[0]), val(o[1])) if len(o) == 2 else (val(o[0]), 0)

            if m in ("push", "push.w"):
                for op in sorted(o, key=lambda x: x.reg, reverse=True):
                    self.reg["sp"] -= 4
                    self._w32(self.reg["sp"], g(rn(op.reg)))
            elif m in ("pop", "pop.w"):
                for op in sorted(o, key=lambda x: x.reg):
                    name = rn(op.reg)
                    v = self._r32(self.reg["sp"])
                    s(name, v & ~1 if name == "pc" else v)
                    self.reg["sp"] += 4
            elif m in ("mov", "movs", "movs.w", "mov.w", "movw", "mvn"):
                src = val(o[1])
                if m == "mvn":
                    src = (~src) & 0xFFFFFFFF
                s(rn(o[0].reg), src)
                if m in ("movs", "mvn"):
                    setflags(src)
            elif m in ("str", "str.w"):
                op = o[1]
                addr = g(rn(op.mem.base)) + op.mem.disp
                self._w32(addr, val(o[0]))
            elif m in ("ldr", "ldr.w"):
                op = o[1]
                if rn(op.mem.base) == "pc":
                    addr = ((pc + 4) & ~3) + op.mem.disp
                else:
                    addr = g(rn(op.mem.base)) + op.mem.disp
                s(rn(o[0].reg), self._word(addr))
            elif m in ("lsrs", "lsrs.w", "lsls"):
                sh = val(o[2])
                if m.startswith("lsrs"):
                    s(rn(o[0].reg), val(o[1]) >> sh)
                else:
                    s(rn(o[0].reg), (val(o[1]) << sh) & 0xFFFFFFFF)
                setflags(g(rn(o[0].reg)))
            elif m in ("cmp", "cmp.w"):
                setflags((val(o[0]) - val(o[1])) & 0xFFFFFFFF)
            elif m in ("ubfx",):
                lsb, width = val(o[2]), val(o[3])
                s(rn(o[0].reg), (val(o[1]) >> lsb) & ((1 << width) - 1))
            elif m.startswith("uxtb"):
                s(rn(o[0].reg), val(o[1]) & 0xFF)
            elif m in ("orr", "orrs"):
                a, b = srcs(o)
                s(rn(o[0].reg), a | b)
            elif m in ("and", "ands"):
                a, b = srcs(o)
                s(rn(o[0].reg), a & b)
                if m == "ands":
                    setflags(g(rn(o[0].reg)))
            elif m in ("add", "adds"):
                a, b = srcs(o)
                s(rn(o[0].reg), a + b)
            elif m in ("sub", "subs"):
                a, b = srcs(o)
                s(rn(o[0].reg), (a - b) & 0xFFFFFFFF)
            elif m in ("adr",):
                s(rn(o[0].reg), ((pc + 4) & ~3) + val(o[1]))
            elif m in ("nop",):
                pass
            elif m == "bx":
                target = val(o[0]) & ~1
                if target in HELPER_NAMES:
                    if not self._call_helper(target, self.reg["lr"]):
                        return self
                else:
                    self.reg["pc"] = target
                    continue
            elif m in ("bl", "blx"):
                lr = (pc + len(ins.bytes)) | 1
                target = val(o[0]) & ~1
                if target in HELPER_NAMES:
                    if not self._call_helper(target, lr):
                        return self
                else:
                    self.reg["lr"] = lr
                    self.reg["pc"] = target
            elif m in ("b", "b.w"):
                self.reg["pc"] = val(o[0])
            elif m in ("beq", "bne", "bhs", "blo", "bhi", "bls", "bge", "blt"):
                z = self.reg.get("_z", 0)
                nn = self.reg.get("_n", 0)
                take = {"beq": z, "bne": not z, "bhs": not nn, "blo": nn,
                        "bhi": not z and not nn, "bls": z or nn,
                        "bge": nn == 0, "blt": nn == 1}[m]
                if take:
                    self.reg["pc"] = val(o[0])
            else:
                raise AssertionError("unhandled instruction %s %s" % (m, ins.op_str))
            if self.reg["pc"] == pc:
                self.reg["pc"] = pc + len(ins.bytes)
        raise AssertionError("stub did not terminate")

    stack = None
    # allocated lazily in _run_stub


def _run_stub(index, state=0):
    from artefacts_build_rc522_tunnel import assemble_stub
    stub = assemble_stub(OPS_STUB_SRC, OPS_STUB_VA)
    emu = _ThumbStub(stub, OPS_STUB_VA)
    emu.stack = bytearray(0x2000)
    emu.helper_ret["state_get"] = state
    return emu.run(index)


def _helper_names(emu):
    return [c[0] for c in emu.calls]


def _op_calls(emu):
    """Calls made by the op itself (drop the entry state_get/state_set pause)."""
    return [c for c in emu.calls if c[0] not in ("state_get", "state_set")]


def test_emulated_ordinary_index_replays_the_stock_call():
    for slot in (0, 1, 2, 3):
        emu = _run_stub(slot)
        assert emu.calls == [], slot                     # no tunnel op ran
        assert emu.exit == ("handler", 0x080144F7), slot  # tail-called 0x08011D74
        assert emu.reply is None


def test_emulated_tunnel_index_replies_and_exits_early():
    # the handler's stock state/slot kick lives at 0x08014512..0x08014528;
    # the stub must leave through the epilogue instead of falling into it
    for packed in (pt.pack_index(0, a1=0x37, reader=0),
                   pt.pack_index(1, a1=0x0D, a2=0, reader=0),
                   pt.pack_index(6, reader=0)):
        emu = _run_stub(packed)
        assert _helper_names(emu)[-1] == "respond", hex(packed)
        assert emu.exit == ("epilogue", EPILOGUE_VA), hex(packed)
        assert emu.reply is not None


def test_emulated_every_tunnel_op_pauses_the_stock_reader():
    """op 0..6 must drive the recognition state to 0 (reader ownership)."""
    for op in range(7):
        emu = _run_stub(pt.pack_index(op, a1=0x37, reader=0), state=8)
        assert emu.calls[0][0] == "state_get", op
        assert emu.state_writes[0] == 0, op
        assert emu.exit == ("epilogue", EPILOGUE_VA), op


def test_emulated_op7_acquires_and_op8_releases():
    emu = _run_stub(pt.pack_index(7, reader=0), state=8)
    assert emu.calls[0][0] == "state_get"
    # pause, drain, re-pause, and the reply keeps the hold (state 0 again)
    assert emu.state_writes == [0, 0, 0, 0]
    assert ("delay", 200, 0, 0, 0) in emu.calls
    assert emu.reply[3] == 8                         # saved state returned
    assert emu.exit == ("epilogue", EPILOGUE_VA)

    emu = _run_stub(pt.pack_index(8, a2=8, reader=0), state=0)
    # entry pause, a2 restore, and the shared reply restores a2 again
    assert emu.state_writes == [0, 8, 8]
    assert emu.reply[3] == 0
    assert emu.exit == ("epilogue", EPILOGUE_VA)


def test_emulated_op0_decodes_reader_reg_and_returns_value():
    packed = pt.pack_index(0, a1=0x37, reader=1)
    emu = _run_stub(packed)
    assert ("read_reg", 1, 0x37, 0, 0) == _op_calls(emu)[0]
    assert _helper_names(emu)[-1] == "respond"
    # read_reg fake return 0xA1 lands in result.code
    assert emu.reply[3] == 0xA1


def test_emulated_sentinel_and_marker_paths():
    emu = _run_stub(STRTOL_OVERFLOW_SENTINEL, state=8)
    assert _op_calls(emu)[0][:3] == ("read_reg", 0, 0x37)   # fixed lossy fallback
    assert emu.state_writes[0] == 0                         # sentinel also pauses
    assert emu.exit == ("epilogue", EPILOGUE_VA)
    # marker: no hardware access, constant 90, still exits early -- and, since
    # round 6, captures/pauses/restores the recognition state like every reply
    emu = _run_stub(1_000_000, state=8)
    assert _helper_names(emu) == ["state_get", "state_set",
                                  "state_set", "respond"]
    assert emu.state_writes == [0, 8]
    assert emu.reply[3] == 90
    assert emu.exit == ("epilogue", EPILOGUE_VA)


def test_emulated_op6_forces_the_antenna_and_selects():
    """op 6 mirrors the firmware bring-up, forces TxControlReg bits 0..1 with
    read-or-write semantics and retries through the firmware's full mode-1
    entry when the first select fails."""
    emu = _run_stub(pt.pack_index(6, reader=1))
    ops = _op_calls(emu)
    shorts = [c[:4] for c in ops]
    names = [c[0] for c in ops]
    # pin/table setup first, then the firmware channel selection:
    # (4, 0) all HIGH, then (reader, 1) LOW, before the soft reset
    assert names[0] == "init_all"
    assert shorts[1] == ("enable", 4, 0, 0)
    assert shorts[2] == ("enable", 1, 1, 0)
    assert names.index("init_all") < names.index("reset")
    # the key fix: TxControlReg |= 0x03 (read-or-write, preserves other bits)
    assert ("set_bits", 1, 0x14, 0x03) in shorts
    assert ("write_reg", 1, 0x14, 0x03) not in shorts
    assert ("config", 1, 0) in [c[:3] for c in ops]
    # final select is the select-only entry 0x0800E314(reader, 0), just before
    # the reply; a success does NOT run the mode-1 retry
    assert [c[:3] for c in ops][-2] == ("bringup", 1, 0)
    assert ("bringup", 1, 1) not in [c[:3] for c in ops]
    assert emu.reply[3] == 0                           # fake 0 = card present
    assert emu.exit == ("epilogue", EPILOGUE_VA)


def test_emulated_op6_failed_select_retries_and_still_restores():
    """A failed select runs the firmware's full mode-1 bring-up once, and the
    failure reply still restores the captured recognition state."""
    from artefacts_build_rc522_tunnel import assemble_stub
    for failure in (2, 0xFF):
        stub = assemble_stub(OPS_STUB_SRC, OPS_STUB_VA)
        emu = _ThumbStub(stub, OPS_STUB_VA)
        emu.stack = bytearray(0x2000)
        emu.helper_ret["state_get"] = 8
        emu.helper_ret["bringup"] = failure
        emu = emu.run(pt.pack_index(6, reader=1))
        bringups = [c[:3] for c in emu.calls if c[0] == "bringup"]
        assert bringups == [("bringup", 1, 0), ("bringup", 1, 1)], failure
        assert emu.state_writes == [0, 8], failure     # restored, not wedged
        assert emu.reply[3] == failure
        assert emu.exit == ("epilogue", EPILOGUE_VA)


def test_every_reply_path_restores_the_captured_state():
    """Fix round 6 walk: success, failure, unknown-op and marker replies all
    put the recognition state captured at entry back (setup/evaluate/cleanup
    included); op 7 keeps the explicit hold and op 8 restores `a2`."""
    packed = [pt.pack_index(op, a1=0x37, reader=0) for op in range(6)]
    packed += [pt.pack_index(9, reader=0),          # unknown op -> 255
               pt.pack_index(0xFF, reader=0)]       # unknown op -> 255
    for index in packed + [STRTOL_OVERFLOW_SENTINEL, 1_000_000]:
        emu = _run_stub(index, state=8)
        assert emu.state_writes[0] == 0, hex(index)     # entry: pause
        assert emu.state_writes[-1] == 8, hex(index)    # reply: restore
        assert emu.exit == ("epilogue", EPILOGUE_VA), hex(index)

    # op 7 keeps the hold: the last write is 0, not the captured state
    emu = _run_stub(pt.pack_index(7, reader=0), state=8)
    assert emu.state_writes[-1] == 0
    assert emu.reply[3] == 8

    # op 8 restores `a2` (and the shared reply restores it again)
    emu = _run_stub(pt.pack_index(8, a2=5, reader=0), state=8)
    assert emu.state_writes[-1] == 5

    # the unknown-op failure branch in detail: 255 + restore
    emu = _run_stub(pt.pack_index(9, reader=0), state=8)
    assert emu.reply[3] == 0xFF
    assert emu.state_writes == [0, 8]


def test_emulated_op1_txmode_brings_up_the_reader_before_the_write():
    """The host's transceive sequence starts with a TxModeReg write; the
    bring-up must happen there, before the host loads its FIFO frame."""
    emu = _run_stub(pt.pack_index(1, a1=0x12, a2=0x80, reader=0))
    ops = _op_calls(emu)
    names = [c[0] for c in ops]
    shorts = [c[:4] for c in ops]
    assert "init_all" in names and "reset" in names and "bringup" in names
    assert ("set_bits", 0, 0x14, 0x03) in shorts      # antenna OR-3
    # the requested TxMode write still lands, and only after the bring-up
    assert shorts[-2] == ("write_reg", 0, 0x12, 0x80)
    assert shorts[-1][0] == "respond"
    assert names.index("bringup") < shorts.index(("write_reg", 0, 0x12, 0x80))
    assert emu.reply[3] == 0
    assert emu.exit == ("epilogue", EPILOGUE_VA)

    # a write to any other register must not reset the chip
    emu = _run_stub(pt.pack_index(1, a1=0x0D, a2=0x00, reader=0))
    assert [c[0] for c in _op_calls(emu)] == ["write_reg", "respond"]


def test_emulated_op3_sets_crc_modes_and_timeout():
    emu = _run_stub(pt.pack_index(3, a1=2, a2=0x0C, reader=0))
    names = _helper_names(emu)
    ops = _op_calls(emu)
    shorts = [c[:4] for c in ops]
    # 0. antenna bits re-asserted (no reset: the pre-loaded FIFO must survive)
    assert ("set_bits", 0, 0x14, 0x03) in shorts
    # 1. TxCRCEn ON: the reader appends the CRC (stock read 0x0800E3E0)
    assert ("set_bits", 0, 0x12, 0x80) in shorts
    assert ("clear_bits", 0, 0x12, 0x80) not in shorts
    # 2. RxCRCEn on (verify + strip the tag CRC)
    assert ("set_bits", 0, 0x13, 0x80) in shorts
    # 3. the firmware's 10 ms timeout, 4. TModeReg |= TAuto
    assert ("timer", 0, 10) in [c[:3] for c in ops]
    assert ("set_bits", 0, 0x2A, 0x80) in shorts
    # 5. Bounded TRANSCEIVE: ComIrq clear, ComIEn, command 0x0C, StartSend
    assert ("write_reg", 0, 0x04, 0x7F) in shorts
    assert ("write_reg", 0, 0x02, 0xF7) in shorts
    assert ("write_reg", 0, 0x01, 0x0C) in shorts
    assert ("write_reg", 0, 0x0D, 0x81) in shorts
    # it waits on ComIrqReg, then clears BitFraming and reads ErrorReg
    assert names.count("read_reg") >= 3
    assert names[-1] == "respond"
    assert emu.exit == ("epilogue", EPILOGUE_VA)


@pytest.mark.parametrize("op,a1,a2,first", [
    (1, 0x0D, 0x00, ("write_reg", 0, 0x0D, 0x00)),
    (2, 0x00, 0x93, ("write_reg", 0, 0x09, 0x93)),
    (4, 0x00, 0x00, ("read_reg", 0, 0x09, 0)),
])
def test_emulated_simple_ops_reply_and_exit(op, a1, a2, first):
    emu = _run_stub(pt.pack_index(op, a1=a1, a2=a2, reader=0))
    assert _op_calls(emu)[0][:4] == first
    assert _helper_names(emu)[-1] == "respond"
    assert emu.exit == ("epilogue", EPILOGUE_VA)


def test_emulated_op5_reads_fifo_level_and_control():
    emu = _run_stub(pt.pack_index(5, a1=0, reader=0))
    assert [c[:3] for c in _op_calls(emu)[:2]] == [("read_reg", 0, 0x0A),
                                                   ("read_reg", 0, 0x0C)]
    assert emu.reply is not None
    assert emu.exit == ("epilogue", EPILOGUE_VA)
