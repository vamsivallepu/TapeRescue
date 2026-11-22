"""UEF exporter and export verification (task 9.4).

Requirements 11.3, 11.4. Byte-array observations only, no WAVs. Blocks come
from ``block_builder`` so header and data CRCs are real; the exported image is
read back with the independent reader.
"""

from __future__ import annotations

import ast
import random
import struct
from pathlib import Path

import pytest

import taperescue
import taperescue.uef.exporter as exporter_mod
import taperescue.uef.verify as verify_mod
from taperescue.engine import recover
from taperescue.engine.gate import export_decision
from taperescue.fixtures.block_builder import TapeBlock, build_blocks
from taperescue.models import (
    BlockObservation,
    BlockResult,
    CaptureObservations,
    CrcReading,
    HeaderFields,
    RecoveryResult,
)
from taperescue.uef.exporter import (
    FIRST_CARRIER_CYCLES,
    OTHER_CARRIER_CYCLES,
    TRAILING_CARRIER_CYCLES,
    ExportRefused,
    build_uef,
    export_uef,
)
from taperescue.uef.reader import parse_uef, read_uef
from taperescue.uef.verify import ExportVerificationFailed, verify_export

CID_A = "a" * 64
CID_B = "b" * 64
PROGRAM = random.Random(94).randbytes(1000)  # 4 blocks: 3 x 256 + 232


def observe(
    cid: str,
    block: TapeBlock,
    *,
    payload: bytes | None = None,
    data_outcome: str | None = None,
    data_crc: int | None = None,
) -> BlockObservation:
    """Observation of ``block`` as a decoder would report it."""
    payload = block.payload if payload is None else payload
    if block.data_crc is None:
        crc = None
    else:
        recorded = block.data_crc if data_crc is None else data_crc
        outcome = data_outcome or ("valid" if payload == block.payload else "invalid")
        crc = CrcReading(outcome, recorded)
    return BlockObservation(
        capture_id=cid,
        sample_offset=100_000 * (block.block_no + 1),
        header_raw=block.header_raw,
        header=HeaderFields(
            block.filename, block.load_addr, block.exec_addr, block.block_no,
            len(block.payload), block.flags, block.next_addr,
        ),
        header_crc=CrcReading("valid", block.header_crc),
        payload=payload,
        expected_len=len(block.payload),
        actual_len=len(payload),
        data_crc=crc,
    )


def capture(cid: str, blocks: list[TapeBlock], **overrides: BlockObservation) -> CaptureObservations:
    obs = [overrides.get(f"b{b.block_no}") or observe(cid, b) for b in blocks]
    return CaptureObservations(cid, tuple(obs))


def flip(data: bytes, offset: int, mask: int) -> bytes:
    b = bytearray(data)
    b[offset] ^= mask
    return bytes(b)


def chunks(image: bytes) -> list[tuple[int, bytes]]:
    pos, out = 12, []
    while pos < len(image):
        cid, length = struct.unpack_from("<HI", image, pos)
        out.append((cid, image[pos + 6 : pos + 6 + length]))
        pos += 6 + length
    return out


def assert_matches_capture(path: Path, blocks: list[TapeBlock], program: bytes) -> None:
    parsed = read_uef(path)
    assert len(parsed) == len(blocks)
    for fb, tb in zip(parsed, blocks):
        assert fb.header_raw == tb.header_raw
        assert fb.header_crc_in_file == tb.header_crc
        assert fb.payload == tb.payload
        assert fb.data_crc_in_file == tb.data_crc
        assert fb.header_crc_ok and fb.data_crc_ok
    assert b"".join(fb.payload for fb in parsed) == program


# --- round trips -----------------------------------------------------------


def test_single_capture_round_trip(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    result = recover([capture(CID_A, blocks)])
    out = tmp_path / "out.uef"
    export_uef(result, export_decision(result), out)
    assert_matches_capture(out, blocks, PROGRAM)
    assert list(tmp_path.iterdir()) == [out]  # no temporary file left behind


def test_image_layout(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    result = recover([capture(CID_A, blocks)])
    image = build_uef(result, export_decision(result))
    assert image[:12] == b"UEF File!\x00" + bytes([10, 0])
    layout = chunks(image)
    assert layout[0] == (0x0000, f"TapeRescue {taperescue.__version__}".encode() + b"\x00")
    carriers = [struct.unpack("<H", d)[0] for c, d in layout if c == 0x0110]
    assert carriers == [FIRST_CARRIER_CYCLES] + [OTHER_CARRIER_CYCLES] * 3 + [TRAILING_CARRIER_CYCLES]
    assert (FIRST_CARRIER_CYCLES, OTHER_CARRIER_CYCLES) == (12240, 2160)
    data = [d for c, d in layout if c == 0x0100]
    assert [c for c, _ in layout[1:-1]] == [0x0110, 0x0100] * 4
    tb = blocks[1]
    assert data[1] == (
        b"\x2a" + tb.header_raw + tb.header_crc.to_bytes(2, "big")
        + tb.payload + tb.data_crc.to_bytes(2, "big")  # type: ignore[union-attr]
    )


def test_empty_block_has_no_data_crc(tmp_path: Path) -> None:
    blocks = build_blocks(b"")
    assert blocks[0].flags == 0xC0
    result = recover([capture(CID_A, blocks)])
    out = tmp_path / "empty.uef"
    export_uef(result, export_decision(result), out)
    (fb,) = read_uef(out)
    assert fb.payload == b"" and fb.data_crc_in_file is None and fb.header_crc_ok


def test_two_captures_use_lowest_capture_id_header(tmp_path: Path) -> None:
    # Same identity key, different next-file address: header bytes differ.
    a_blocks = build_blocks(PROGRAM)
    b_blocks = build_blocks(PROGRAM, next_addr=0x1234)
    a2 = a_blocks[2]
    damaged = observe(CID_A, a2, payload=flip(a2.payload, 9, 0x10))
    cap_a = capture(CID_A, a_blocks, b2=damaged)
    cap_b = capture(CID_B, b_blocks)
    for order in ([cap_a, cap_b], [cap_b, cap_a]):
        result = recover(order)
        assert result.blocks[2].supporting_capture_ids == (CID_B,)
        out = tmp_path / "two.uef"
        export_uef(result, export_decision(result), out)
        # Block 2 is intact only in B but its header still comes from A.
        assert_matches_capture(out, a_blocks, PROGRAM)


def test_reconstructed_block_uses_recorded_crc(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    tb = blocks[1]
    read_a = observe(CID_A, tb, payload=flip(tb.payload, 17, 0x04))
    read_b = observe(CID_B, tb, payload=flip(tb.payload, 64, 0x01))
    base = recover([capture(CID_A, blocks, b1=read_a), capture(CID_B, blocks, b1=read_b)])
    assert base.blocks[1].status == "Reconstructed · CRC match"
    assert base.blocks[1].payload == tb.payload
    decision = export_decision(base)
    assert decision.permitted and decision.reconstructed_blocks == (1,)
    out = tmp_path / "rec.uef"
    export_uef(base, decision, out)
    assert_matches_capture(out, blocks, PROGRAM)


# --- refusal -----------------------------------------------------------------


def test_refused_export_writes_nothing(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    tb = blocks[2]
    damaged = observe(CID_A, tb, payload=flip(tb.payload, 0, 0x01))
    result = recover([capture(CID_A, blocks, b2=damaged)])
    decision = export_decision(result)
    assert not decision.permitted
    out = tmp_path / "refused.uef"
    with pytest.raises(ExportRefused) as info:
        export_uef(result, decision, out)
    assert info.value.blockers == decision.blockers == ((2, "single_read"),)
    assert list(tmp_path.iterdir()) == []


def test_stale_permitted_decision_does_not_unlock_export(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    good = recover([capture(CID_A, blocks)])
    tb = blocks[3]
    bad = recover([capture(CID_A, blocks, b3=observe(CID_A, tb, payload=flip(tb.payload, 5, 0x02)))])
    with pytest.raises(ExportRefused) as info:
        export_uef(bad, export_decision(good), tmp_path / "x.uef")
    assert info.value.blockers == ((3, "single_read"),)
    # A refused decision is honoured even when the result itself would pass.
    with pytest.raises(ExportRefused):
        export_uef(good, export_decision(bad), tmp_path / "y.uef")
    assert list(tmp_path.iterdir()) == []


# --- verification ------------------------------------------------------------


def test_tampered_recorded_crc_fails_verification_and_deletes(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    tb = blocks[1]
    wrong = tb.data_crc ^ 0x0001  # type: ignore[operator]
    tampered = observe(CID_A, tb, data_outcome="valid", data_crc=wrong)
    result = recover([capture(CID_A, blocks, b1=tampered)])
    assert export_decision(result).permitted
    # The exporter writes the recorded value as is; it does not recompute it.
    image = build_uef(result, export_decision(result))
    assert parse_uef(image)[1].data_crc_in_file == wrong
    out = tmp_path / "tampered.uef"
    with pytest.raises(ExportVerificationFailed) as info:
        export_uef(result, export_decision(result), out)
    assert any("block 1: data CRC does not match" in p for p in info.value.problems)
    assert list(tmp_path.iterdir()) == []


def test_verify_detects_modified_file_and_deletes(tmp_path: Path) -> None:
    blocks = build_blocks(PROGRAM)
    result = recover([capture(CID_A, blocks)])
    out = tmp_path / "mod.uef"
    export_uef(result, export_decision(result), out)
    image = bytearray(out.read_bytes())
    image[-40] ^= 0x01  # inside block 3's payload, before its CRC and the trailer
    out.write_bytes(bytes(image))
    with pytest.raises(ExportVerificationFailed) as info:
        verify_export(out, result)
    assert any("block 3" in p for p in info.value.problems)
    assert not out.exists()


def test_verify_rejects_crc_that_was_not_recorded(tmp_path: Path) -> None:
    # A file whose CRCs are self-consistent but differ from the captures fails.
    blocks = build_blocks(PROGRAM)
    other = build_blocks(PROGRAM, next_addr=0x99)
    result = recover([capture(CID_A, blocks)])
    other_result = recover([capture(CID_A, other)])
    out = tmp_path / "other.uef"
    out.write_bytes(build_uef(other_result, export_decision(other_result)))
    with pytest.raises(ExportVerificationFailed) as info:
        verify_export(out, result)
    assert any("header CRC in file" in p for p in info.value.problems)
    assert not out.exists()


def test_verify_rejects_unreadable_file(tmp_path: Path) -> None:
    result = recover([capture(CID_A, build_blocks(PROGRAM))])
    out = tmp_path / "junk.uef"
    out.write_bytes(b"not a uef")
    with pytest.raises(ExportVerificationFailed):
        verify_export(out, result)
    assert not out.exists()


# --- independence --------------------------------------------------------------


def _imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_exporter_never_computes_crcs() -> None:
    imports = _imports(exporter_mod)
    assert "taperescue.crc" not in imports
    assert "taperescue.uef.reader" not in imports
    assert "zlib" not in imports and "binascii" not in imports
    assert "crc16" not in Path(exporter_mod.__file__).read_text()


def test_verify_does_not_import_exporter_or_crc() -> None:
    imports = _imports(verify_mod)
    assert "taperescue.uef.exporter" not in imports
    assert "taperescue.crc" not in imports
    assert "taperescue.uef.reader" in imports
