"""Export gate (task 9.2).

Requirements 11.1, 11.2. Byte-array observations only, no WAVs. Most cases go
through ``recover``; cases the engine cannot produce (a block whose header
number differs from its position, a Reconstructed block before task 12.2) use a
hand-built ``RecoveryResult``.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from taperescue.engine import recover
from taperescue.engine.gate import ExportDecision, export_decision
from taperescue.models import (
    BlockObservation,
    BlockResult,
    CaptureObservations,
    CrcReading,
    HeaderFields,
    RecoverOptions,
    RecoveryResult,
)

CID_A = "a" * 64
CID_B = "b" * 64
FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023


def payload_for(block_no: int, length: int = 256) -> bytes:
    return bytes((block_no * 31 + i) % 256 for i in range(length))


def read(
    cid: str,
    block_no: int,
    *,
    length: int = 256,
    data_ok: bool = True,
    final: bool = False,
    filename: str = FILENAME,
    load: int = LOAD,
    exec_: int = EXEC,
    header_block_no: int | None = None,
) -> BlockObservation:
    payload = payload_for(block_no, length)
    flags = (0x80 if final else 0) | (0x40 if not payload else 0)
    hdr_no = block_no if header_block_no is None else header_block_no
    hdr = HeaderFields(filename, load, exec_, hdr_no, length, flags, 0)
    raw = (
        filename.encode() + b"\x00"
        + load.to_bytes(4, "little") + exec_.to_bytes(4, "little")
        + hdr_no.to_bytes(2, "little") + length.to_bytes(2, "little")
        + bytes([flags]) + bytes(4)
    )
    return BlockObservation(
        capture_id=cid,
        sample_offset=10_000 * (block_no + 1),
        header_raw=raw,
        header=hdr,
        header_crc=CrcReading("valid", 0x1111),
        payload=payload,
        expected_len=length,
        actual_len=length,
        data_crc=None if not payload else CrcReading("valid" if data_ok else "invalid", 0x2222),
    )


def file_reads(cid: str = CID_A, count: int = 4, last_len: int = 100) -> list[BlockObservation]:
    return [
        read(cid, n, length=last_len if n == count - 1 else 256, final=n == count - 1)
        for n in range(count)
    ]


def decide(*reads: BlockObservation) -> ExportDecision:
    return export_decision(recover([CaptureObservations(CID_A, tuple(reads))]))


def intact(obs: BlockObservation, position: int | None = None) -> BlockResult:
    return BlockResult(
        block_no=obs.header.block_no if position is None else position,  # type: ignore[union-attr]
        status="Intact",
        reason=None,
        payload=obs.payload,
        supporting_capture_ids=(obs.capture_id,),
        reads=(obs,),
        conflict=None,
        reconstruction=None,
    )


def hand_built(blocks: list[BlockResult], final_block: int | None) -> RecoveryResult:
    obs = tuple(r for b in blocks for r in b.reads)
    return RecoveryResult(
        captures=(CaptureObservations(CID_A, obs),),
        duplicates_merged=(),
        anchors=(),
        final_block=final_block,
        blocks=tuple(blocks),
        options=RecoverOptions(),
    )


# --- permitted ---------------------------------------------------------------


def test_all_intact_is_permitted():
    decision = decide(*file_reads())
    assert decision == ExportDecision(permitted=True, blockers=(), reconstructed_blocks=())


def test_empty_final_block_is_permitted():
    decision = decide(*file_reads(count=3, last_len=0))
    assert decision.permitted


def test_reconstructed_block_is_permitted_and_listed():
    reads = file_reads(count=3)
    blocks = [intact(r) for r in reads]
    blocks[1] = dataclasses.replace(blocks[1], status="Reconstructed · CRC match")
    decision = export_decision(hand_built(blocks, final_block=2))
    assert decision == ExportDecision(permitted=True, blockers=(), reconstructed_blocks=(1,))


# --- refused -----------------------------------------------------------------


def test_missing_block_is_a_blocker():
    reads = file_reads()
    decision = decide(reads[0], reads[1], reads[3])
    assert not decision.permitted
    assert decision.blockers == ((2, "block_missing"),)


def test_crc_failed_block_is_a_blocker():
    reads = file_reads()
    reads[1] = read(CID_A, 1, data_ok=False)
    decision = decide(*reads)
    assert not decision.permitted
    assert decision.blockers == ((1, "single_read"),)


def test_crc_failed_without_engine_reason_still_blocks():
    reads = file_reads(count=2)
    blocks = [intact(r) for r in reads]
    blocks[0] = dataclasses.replace(blocks[0], status="CRC failed", payload=None)
    decision = export_decision(hand_built(blocks, final_block=1))
    assert decision.blockers == ((0, "crc_failed"),)


def test_no_final_flag_is_a_blocker():
    reads = [read(CID_A, n) for n in range(3)]
    decision = decide(*reads)
    assert not decision.permitted
    assert decision.blockers == ((3, "block_missing"),)


def test_no_reads_at_all_is_refused():
    decision = export_decision(recover([CaptureObservations(CID_A, ())]))
    assert decision.blockers == ((0, "block_missing"),)


def test_final_flag_on_non_final_block_via_recover():
    # Block 1 is flagged but blocks 2 and 3 of the same file follow it.
    reads = file_reads()
    reads[1] = read(CID_A, 1, final=True)
    decision = decide(*reads)
    assert not decision.permitted
    assert decision.blockers == ((1, "unexpected_final_flag"),)


def test_final_flag_on_non_final_block_hand_built():
    reads = file_reads()
    reads[1] = read(CID_A, 1, final=True)
    decision = export_decision(hand_built([intact(r) for r in reads], final_block=3))
    assert decision.blockers == ((1, "unexpected_final_flag"),)


def test_final_block_without_flag_is_a_blocker():
    reads = file_reads()
    reads[3] = read(CID_A, 3, length=100)
    decision = export_decision(hand_built([intact(r) for r in reads], final_block=3))
    assert decision.blockers == ((3, "final_flag_missing"),)


def test_mismatched_filename_is_a_blocker():
    reads = file_reads()
    reads[2] = read(CID_A, 2, filename="OTHER")
    decision = decide(*reads)
    assert decision.blockers == ((2, "filename_mismatch"),)


def test_mismatched_load_address_is_a_blocker():
    reads = file_reads()
    reads[0] = read(CID_A, 0, load=0x1900)
    decision = decide(*reads)
    assert decision.blockers == ((0, "address_mismatch"),)


def test_mismatched_exec_address_is_a_blocker():
    reads = file_reads()
    reads[3] = read(CID_A, 3, length=100, final=True, exec_=0x1900)
    decision = decide(*reads)
    assert decision.blockers == ((3, "address_mismatch"),)


def test_block_no_mismatch_is_a_blocker():
    reads = file_reads()
    wrong = read(CID_A, 2, header_block_no=5)
    blocks = [intact(r) for r in reads]
    blocks[2] = intact(wrong, position=2)
    decision = export_decision(hand_built(blocks, final_block=3))
    assert decision.blockers == ((2, "block_no_mismatch"),)


def test_short_non_final_block_is_a_blocker():
    reads = file_reads()
    reads[1] = read(CID_A, 1, length=200)
    decision = decide(*reads)
    assert decision.blockers == ((1, "block_len_invalid"),)


def test_blockers_are_sorted_by_block_number():
    reads = file_reads(count=6)
    reads[4] = read(CID_A, 4, data_ok=False)
    reads[1] = read(CID_A, 1, filename="OTHER")
    decision = decide(reads[5], reads[4], reads[3], reads[1], reads[0])
    assert decision.blockers == (
        (1, "filename_mismatch"),
        (2, "block_missing"),
        (4, "single_read"),
    )


def test_two_capture_result_with_intact_blocks_is_permitted():
    a = file_reads(CID_A)
    b = file_reads(CID_B)
    a[1] = read(CID_A, 1, data_ok=False)
    b[2] = read(CID_B, 2, data_ok=False)
    result = recover([CaptureObservations(CID_B, tuple(b)), CaptureObservations(CID_A, tuple(a))])
    assert export_decision(result).permitted


# --- purity ------------------------------------------------------------------


def test_gate_imports_only_models():
    import taperescue.engine.gate as gate

    tree = ast.parse(Path(gate.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    project = {m for m in imported if m.startswith("taperescue")}
    assert project == {"taperescue.models"}
