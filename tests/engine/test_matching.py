"""Capture matching, anchors, and conflict steps c and d (task 11.1).

Requirements 7.1-7.6 and 8.5. Byte-array observations only, no WAVs.
"""

from __future__ import annotations

import pytest

from taperescue.engine import DECISION_STEPS, PairingRejected, recover
from taperescue.engine.matching import (
    NO_ANCHOR_MESSAGE,
    compute_anchors,
    identity_key,
    step_c_header_disagreement,
    step_d_payload_conflict,
)
from taperescue.engine.selection import step_b_header_invalid, step_e_intact
from taperescue.models import (
    BlockObservation,
    CaptureObservations,
    CrcReading,
    HeaderFields,
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
    payload: bytes | None = None,
    data_ok: bool = True,
    header_ok: bool = True,
    final: bool = False,
    load: int = LOAD,
    next_addr: int = 0,
    offset: int | None = None,
) -> BlockObservation:
    payload = payload_for(block_no) if payload is None else payload
    flags = 0x80 if final else 0
    raw = (
        FILENAME.encode() + b"\x00"
        + load.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + len(payload).to_bytes(2, "little")
        + bytes([flags]) + next_addr.to_bytes(4, "little")
    )
    hdr = HeaderFields(FILENAME, load, EXEC, block_no, len(payload), flags, next_addr)
    return BlockObservation(
        capture_id=cid,
        sample_offset=offset if offset is not None else 10_000 * (block_no + 1),
        header_raw=raw,
        header=hdr if header_ok else None,
        header_crc=CrcReading("valid" if header_ok else "invalid", 0x1111),
        payload=payload,
        expected_len=len(payload),
        actual_len=len(payload),
        data_crc=CrcReading("valid" if data_ok else "invalid", 0x2222),
    )


def file_reads(cid: str, count: int = 4, **overrides: BlockObservation) -> list[BlockObservation]:
    """Intact reads of blocks 0..count-1, with ``b<n>=...`` replacing block n."""
    reads = [read(cid, n, final=(n == count - 1)) for n in range(count)]
    for name, obs in overrides.items():
        reads[int(name[1:])] = obs
    return reads


def capture(cid: str, reads) -> CaptureObservations:
    return CaptureObservations(cid, tuple(reads))


def test_steps_c_and_d_sit_between_b_and_e():
    steps = list(DECISION_STEPS)
    b, c, d, e = (steps.index(s) for s in (step_b_header_invalid, step_c_header_disagreement,
                                            step_d_payload_conflict, step_e_intact))
    assert b < c < d < e


# --- identity (Requirements 7.1, 7.2) ----------------------------------------


def test_identity_key_only_for_valid_headers():
    assert identity_key(read(CID_A, 0)) == (FILENAME, LOAD, EXEC, 0, 256, 0)
    assert identity_key(read(CID_A, 0, header_ok=False)) is None


def test_next_addr_difference_alone_does_not_break_identity():
    a = file_reads(CID_A, b2=read(CID_A, 2, next_addr=0x1234))
    b = file_reads(CID_B, b2=read(CID_B, 2, next_addr=0x5678))
    result = recover([capture(CID_A, a), capture(CID_B, b)])
    assert result.anchors == (0, 1, 2, 3)
    block2 = result.blocks[2]
    assert (block2.status, block2.reason, block2.conflict) == ("Intact", None, None)
    assert block2.supporting_capture_ids == (CID_A, CID_B)


def test_invalid_header_read_supplies_no_anchor():
    # B's only intact-looking read of block 0 has an invalid header CRC.
    a = file_reads(CID_A, count=2)
    b = [read(CID_B, 0, header_ok=False), read(CID_B, 1, data_ok=False, final=True)]
    with pytest.raises(PairingRejected):
        recover([capture(CID_A, a), capture(CID_B, b)])


# --- anchors (Requirements 7.3, 7.4) -----------------------------------------


def test_anchors_found_and_sorted():
    a = file_reads(CID_A, count=6, b1=read(CID_A, 1, data_ok=False))
    b = file_reads(CID_B, count=6, b4=read(CID_B, 4, data_ok=False))
    # Supply B's reads in reverse order to check the anchors do not depend on it.
    result = recover([capture(CID_B, reversed(b)), capture(CID_A, a)])
    assert result.anchors == (0, 2, 3, 5)
    assert compute_anchors(result.captures) == (0, 2, 3, 5)


def test_no_shared_anchor_is_rejected_with_design_message():
    # Every block is intact in at least one capture, but none in both.
    a = [read(CID_A, n, data_ok=(n % 2 == 0), final=(n == 3)) for n in range(4)]
    b = [read(CID_B, n, data_ok=(n % 2 == 1), final=(n == 3)) for n in range(4)]
    with pytest.raises(PairingRejected) as exc:
        recover([capture(CID_A, a), capture(CID_B, b)])
    assert exc.value.code == "no_anchor"
    assert exc.value.message == NO_ANCHOR_MESSAGE
    assert str(exc.value) == (
        "These recordings cannot be shown to belong to the same program. "
        "Select recordings of the same program version."
    )


def test_intact_reads_of_different_programs_are_not_anchors():
    a = file_reads(CID_A)
    b = [read(CID_B, n, payload=payload_for(n + 10), final=(n == 3)) for n in range(4)]
    with pytest.raises(PairingRejected):
        recover([capture(CID_A, a), capture(CID_B, b)])


def test_single_capture_never_raises_no_anchor():
    a = [read(CID_A, n, data_ok=False, final=(n == 1)) for n in range(2)]
    result = recover([capture(CID_A, a)])
    assert result.anchors == ()
    assert [b.reason for b in result.blocks] == ["single_read", "single_read"]


def test_duplicate_capture_is_one_capture_and_needs_no_anchor():
    cap = capture(CID_A, [read(CID_A, 0, data_ok=False, final=True)])
    result = recover([cap, cap])
    assert result.duplicates_merged == (CID_A,)
    assert result.anchors == ()


# --- step c: header disagreement (Requirement 7.5) ---------------------------


def test_header_disagreement_on_load_address():
    a = file_reads(CID_A)
    b = file_reads(CID_B, b3=read(CID_B, 3, load=0xFFFF1900, final=True))
    result = recover([capture(CID_A, a), capture(CID_B, b)])
    assert result.anchors == (0, 1, 2)
    block3 = result.blocks[3]
    assert (block3.status, block3.reason) == ("Conflict", "header_disagreement")
    assert block3.conflict == {"kind": "header", "capture_ids": [CID_A, CID_B]}
    assert block3.payload is None
    assert block3.supporting_capture_ids == ()
    assert block3.reconstruction is None  # excluded from reconstruction
    assert result.program_payload() is None


def test_header_disagreement_with_failed_reads_is_still_a_conflict():
    # Neither read is intact, so without step c the block would go on to
    # reconstruction; the mismatched trusted headers must stop it first.
    a = file_reads(CID_A, b2=read(CID_A, 2, data_ok=False))
    b = file_reads(CID_B, b2=read(CID_B, 2, data_ok=False, load=0xFFFF1900))
    block2 = recover([capture(CID_A, a), capture(CID_B, b)]).blocks[2]
    assert (block2.status, block2.reason) == ("Conflict", "header_disagreement")
    assert block2.reconstruction is None


def test_invalid_header_read_does_not_cause_disagreement():
    # B's re-read of block 2 has a bad header CRC and a different raw load address.
    bad = read(CID_B, 2, header_ok=False, load=0xFFFF1900, offset=25_000)
    a = file_reads(CID_A)
    b = file_reads(CID_B) + [bad]
    block2 = recover([capture(CID_A, a), capture(CID_B, b)]).blocks[2]
    assert block2.status == "Intact"
    assert bad in block2.reads


# --- step d: payload conflict (Requirement 7.6) ------------------------------


def test_conflicting_intact_payloads():
    other = bytes(b ^ 0xFF for b in payload_for(2))
    a = file_reads(CID_A)
    b = file_reads(CID_B, b2=read(CID_B, 2, payload=other))
    result = recover([capture(CID_B, b), capture(CID_A, a)])
    assert 2 not in result.anchors
    block2 = result.blocks[2]
    assert block2.status == "Conflict"
    assert block2.reason is None
    assert block2.conflict == {"kind": "payload", "capture_ids": [CID_A, CID_B]}
    assert block2.payload is None
    assert block2.supporting_capture_ids == ()
    assert block2.reconstruction is None
    assert result.program_payload() is None


# --- Requirement 8.5 still holds --------------------------------------------


def test_intact_blocks_sourced_from_different_captures():
    a = [read(CID_A, n, data_ok=(n != 6), final=(n == 7)) for n in range(8)]
    b = [read(CID_B, n, data_ok=(n != 4), final=(n == 7)) for n in range(8)]
    result = recover([capture(CID_A, a), capture(CID_B, b)])
    assert result.anchors == (0, 1, 2, 3, 5, 7)
    assert all(block.status == "Intact" for block in result.blocks)
    assert result.blocks[4].supporting_capture_ids == (CID_A,)
    assert result.blocks[6].supporting_capture_ids == (CID_B,)
    assert result.program_payload() == b"".join(payload_for(n) for n in range(8))
