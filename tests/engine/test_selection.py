"""Engine skeleton and intact selection (task 9.1).

Requirements 3.6, 8.1, 8.2, 8.3, 8.4, 9.11. Byte-array observations only, no WAVs.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from taperescue.engine import RecoverOptions, recover
from taperescue.models import (
    BlockObservation,
    CaptureObservations,
    CrcReading,
    HeaderFields,
)
from taperescue.models import RecoverOptions as ModelRecoverOptions

CID_A = "a" * 64
CID_B = "b" * 64
FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023


def payload_for(block_no: int, length: int = 256) -> bytes:
    return bytes((block_no * 31 + i) % 256 for i in range(length))


def header_raw(block_no: int, block_len: int, flags: int) -> bytes:
    return (
        FILENAME.encode() + b"\x00"
        + LOAD.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + block_len.to_bytes(2, "little")
        + bytes([flags]) + (0).to_bytes(4, "little")
    )


def read(
    cid: str,
    block_no: int,
    *,
    payload: bytes | None = None,
    data_ok: bool = True,
    header_ok: bool = True,
    final: bool = False,
    offset: int | None = None,
) -> BlockObservation:
    payload = payload_for(block_no) if payload is None else payload
    flags = 0x80 if final else 0
    if not payload:
        flags |= 0x40
    hdr = HeaderFields(FILENAME, LOAD, EXEC, block_no, len(payload), flags, 0)
    return BlockObservation(
        capture_id=cid,
        sample_offset=offset if offset is not None else 10_000 * (block_no + 1),
        header_raw=header_raw(block_no, len(payload), flags),
        header=hdr if header_ok else None,
        header_crc=CrcReading("valid" if header_ok else "invalid", 0x1111),
        payload=payload,
        expected_len=len(payload),
        actual_len=len(payload),
        data_crc=None if not payload else CrcReading("valid" if data_ok else "invalid", 0x2222),
    )


def capture(cid: str, *reads: BlockObservation) -> CaptureObservations:
    return CaptureObservations(cid, tuple(reads))


def statuses(result):
    return [(b.block_no, b.status, b.reason) for b in result.blocks]


def test_recover_options_is_the_model_class():
    assert RecoverOptions is ModelRecoverOptions
    assert RecoverOptions().candidate_cap == 64


def test_single_intact_capture():
    cap = capture(CID_A, *(read(CID_A, n, final=(n == 3)) for n in range(4)))
    result = recover([cap])
    assert result.final_block == 3
    assert statuses(result) == [(n, "Intact", None) for n in range(4)]
    for b in result.blocks:
        assert b.payload == payload_for(b.block_no)
        assert b.supporting_capture_ids == (CID_A,)
    assert result.program_payload() == b"".join(payload_for(n) for n in range(4))
    assert result.duplicates_merged == ()


def test_lowest_flagged_block_ends_the_range():
    cap = capture(CID_A, *(read(CID_A, n, final=n in (2, 4)) for n in range(5)))
    result = recover([cap])
    assert result.final_block == 2
    assert [b.block_no for b in result.blocks] == [0, 1, 2]


def test_missing_block():
    cap = capture(CID_A, read(CID_A, 0), read(CID_A, 2, final=True))
    result = recover([cap])
    assert statuses(result)[1] == (1, "Missing", "block_missing")
    assert result.blocks[1].payload is None
    assert result.blocks[1].reads == ()
    assert result.program_payload() is None


def test_header_invalid_only_block():
    bad = read(CID_A, 1, header_ok=False)
    cap = capture(CID_A, read(CID_A, 0), bad, read(CID_A, 2, final=True))
    result = recover([cap])
    block1 = result.blocks[1]
    assert (block1.status, block1.reason) == ("Unresolved", "header_invalid")
    assert block1.reads == (bad,)
    assert block1.payload is None
    assert block1.supporting_capture_ids == ()


def test_header_invalid_read_placed_from_neighbours_when_raw_header_is_garbage():
    bad = read(CID_A, 1, header_ok=False)
    bad = BlockObservation(**{**bad.__dict__, "header_raw": b"\xff" * 5})
    cap = capture(CID_A, read(CID_A, 0), bad, read(CID_A, 2, final=True))
    block1 = recover([cap]).blocks[1]
    assert (block1.status, block1.reason) == ("Unresolved", "header_invalid")
    assert block1.reads == (bad,)


def test_empty_valid_block_is_intact():
    cap = capture(CID_A, read(CID_A, 0), read(CID_A, 1, payload=b"", final=True))
    result = recover([cap])
    block1 = result.blocks[1]
    assert (block1.status, block1.payload) == ("Intact", b"")
    assert block1.supporting_capture_ids == (CID_A,)
    assert result.program_payload() == payload_for(0)


def test_crc_failure_in_single_capture_is_single_read():
    cap = capture(CID_A, read(CID_A, 0), read(CID_A, 1, data_ok=False, final=True))
    block1 = recover([cap]).blocks[1]
    assert (block1.status, block1.reason) == ("CRC failed", "single_read")
    assert block1.payload is None
    assert block1.reconstruction is None


def test_intact_supporting_ids_list_every_capture():
    a = capture(CID_A, read(CID_A, 0), read(CID_A, 1, final=True))
    b = capture(CID_B, read(CID_B, 0), read(CID_B, 1, data_ok=False, final=True))
    result = recover([a, b])
    assert result.blocks[0].supporting_capture_ids == (CID_A, CID_B)
    assert result.blocks[1].status == "Intact"
    assert result.blocks[1].supporting_capture_ids == (CID_A,)


def test_intact_blocks_sourced_from_different_captures():
    # Requirement 8.5: A has intact 4 and failed 6, B has failed 4 and intact 6.
    def reads(cid, failed):
        return [read(cid, n, data_ok=(n != failed), final=(n == 7)) for n in range(8)]

    result = recover([capture(CID_A, *reads(CID_A, 6)), capture(CID_B, *reads(CID_B, 4))])
    assert all(b.status == "Intact" for b in result.blocks)
    assert result.blocks[4].supporting_capture_ids == (CID_A,)
    assert result.blocks[6].supporting_capture_ids == (CID_B,)
    assert result.program_payload() == b"".join(payload_for(n) for n in range(8))


def test_multi_capture_failure_is_never_recovered():
    # Identical reads whose recorded CRC (0x2222) does not match: the one
    # candidate fails, so the block stays unrecovered (step l, task 12.2).
    a = capture(CID_A, read(CID_A, 0), read(CID_A, 1, data_ok=False, final=True))
    b = capture(CID_B, read(CID_B, 0), read(CID_B, 1, data_ok=False, final=True))
    block1 = recover([a, b]).blocks[1]
    assert (block1.status, block1.reason) == ("Unresolved", "no_match")
    assert block1.payload is None


def test_duplicate_capture_ids_collapse():
    cap = capture(CID_A, read(CID_A, 0), read(CID_A, 1, data_ok=False, final=True))
    single = recover([cap])
    doubled = recover([cap, cap])
    assert doubled.duplicates_merged == (CID_A,)
    assert len(doubled.captures) == 1
    assert doubled.blocks == single.blocks
    # The duplicate does not turn one recording into two reads.
    assert doubled.blocks[1].reason == "single_read"


def test_input_order_does_not_change_results():
    a_reads = [read(CID_A, n, data_ok=(n != 2), final=(n == 3)) for n in range(4)]
    a_reads.append(read(CID_A, 1, header_ok=False, offset=15_000))
    b_reads = [read(CID_B, n, data_ok=(n != 1), final=(n == 3)) for n in range(4)]
    results = []
    for a_perm in itertools.permutations(a_reads):
        for caps in ([capture(CID_A, *a_perm), capture(CID_B, *reversed(b_reads))],
                     [capture(CID_B, *b_reads), capture(CID_A, *a_perm)]):
            results.append(recover(caps))
    first = results[0]
    assert [c.capture_id for c in first.captures] == [CID_A, CID_B]
    assert all(r == first for r in results)
    # Captures' reads come out sorted by (block_no, sample_offset).
    # The header-invalid read sits at block 1 and sorts before block 1's valid read.
    offsets = [o.sample_offset for o in first.captures[0].observations]
    assert offsets == [10_000, 15_000, 20_000, 30_000, 40_000]
    assert [r.sample_offset for r in first.blocks[1].reads] == [15_000, 20_000, 20_000]


def test_recover_rejects_empty_input():
    with pytest.raises(ValueError):
        recover([])


def test_engine_imports_only_models_and_engine_modules():
    forbidden = ("taperescue.decoder_adapter", "taperescue.wavcheck", "taperescue.fixtures",
                 "taperescue.uef", "server", "subprocess")
    engine_dir = Path(__file__).resolve().parents[2] / "taperescue" / "engine"
    for path in engine_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in forbidden), (
                    f"{path.name} imports {name}"
                )
