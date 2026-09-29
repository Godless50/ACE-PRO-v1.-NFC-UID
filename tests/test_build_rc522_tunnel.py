# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tests for artefacts_build_rc522_tunnel.py.
# Copyright (C) 2026 ACE-UID contributors
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from artefacts_build_rc522_tunnel import (  # noqa: E402
    BASE_VA,
    PARSER_STUB_SRC,
    assemble_stub,
    build,
)


@pytest.fixture(autouse=True)
def _repo_root(monkeypatch):
    # The plan's test uses paths relative to the repository root.
    monkeypatch.chdir(ROOT)


def test_rebuild_reproduces_current_image(tmp_path):
    out = tmp_path / "rebuild.bin"
    res = build("ACE_V1.3.863_20260716.bin", str(out), hook_fixed=True,
                stub_src="artefacts_stub4.s", version="1.3.863")
    assert res["md5"] == "6148cfc52431fc235536c6d64b5334ef"
    assert res["size"] == 113828
    assert res["crc16"] == 0xAE92


def test_rebuild_is_byte_identical_to_recorded_image(tmp_path):
    out = tmp_path / "rebuild.bin"
    build("ACE_V1.3.863_20260716.bin", str(out), hook_fixed=True,
          version="1.3.863")
    assert out.read_bytes() == (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()


def test_rebuild_diff_is_hook_plus_appended_stub(tmp_path):
    out = tmp_path / "rebuild.bin"
    res = build("ACE_V1.3.863_20260716.bin", str(out), hook_fixed=True,
                version="1.3.863")
    # Exactly the 4 patched hook bytes + the 108 appended stub bytes.
    assert res["changed_bytes"] == 4 + 108
    assert res["stub_size"] == 108
    assert res["diff_ranges"] == [
        [0x08016B9A, 0x08016B9D],
        [BASE_VA + 113720, BASE_VA + 113720 + 107],
    ]


def test_hook_original_contract(tmp_path):
    res = build("ACE_V1.3.863_20260716.bin", str(tmp_path / "h.bin"),
                hook_va=0x08016B9A, hook_fixed=True)
    assert res["hook_original"] == "f8931036"
    assert res["hook_bytes"] == "0df04df8"


def test_canonical_stub_source_assembles_to_recorded_tail():
    recorded = (ROOT / "ACE_V1.3.863_cfw_uid.bin").read_bytes()[113720:]
    assert len(recorded) == 108
    assert assemble_stub(PARSER_STUB_SRC, BASE_VA + 113720) == recorded


def test_stub_src_mismatch_is_reported_not_silently_applied(tmp_path):
    # artefacts_stub4.s is the stock-route stub, not this image's tail; the
    # frozen production stub must still be the one appended.
    res = build("ACE_V1.3.863_20260716.bin", str(tmp_path / "m.bin"),
                hook_fixed=True, stub_src="artefacts_stub4.s")
    # Either it fails to link (undefined orig_snprintf/ret_addr) or it links to
    # something else; both are reported, neither is applied.
    assert any("artefacts_stub4.s" in note for note in res["notes"])
    assert res["md5"] == "6148cfc52431fc235536c6d64b5334ef"


def test_generic_mode_requires_stub_src(tmp_path):
    with pytest.raises(ValueError):
        build("ACE_V1.3.863_20260716.bin", str(tmp_path / "g.bin"),
              hook_fixed=False)
