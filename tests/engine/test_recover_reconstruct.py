"""Reconstruction wired into ``recover`` (task 12.2): decision steps g to l.

Byte-array ``CaptureObservations`` exercise each step, then an end-to-end run
decodes the damaged STARMAP WAVs with the patched decoder (skipped when it is
not built).

Requirements 9.1, 9.8, 9.9, 9.10, 9.12, 14.2 (and 9.5, 12.1 through recover).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from taperescue.crc import crc16_xmodem
from taperescue.engine import RecoverOptions, recover
from taperescue.engine.gate import export_decision
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields

CID_A = "a" * 64
CID_B = "b" * 64
FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023

TEST_PAYLOAD = bytes((17 * i + 11 * i * i + 42) % 256 for i in range(256))
TP_CRC = crc16_xmodem(TEST_PAYLOAD)
FAILED_BLOCK = 1
TAIL = bytes(range(40))  # payload of final block 2


def xor(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


def header_raw(block_no: int, block_len: int, flags: int) -> bytes:
    return (
        FILENAME.encode() + b"\x00"
        + LOAD.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + block_len.to_bytes(2, "little")
        + bytes([flags]) + (0).to_bytes(4, "little")
    )


def obs(
    cid: str,
    block_no: int,
    payload: bytes,
    *,
    data_outcome: str = "valid",
    recorded: int | None = None,
    expected_len: int | None = None,
    header_ok: bool = True,
    final: bool = False,
    load: int = LOAD,
) -> BlockObservation:
    expected_len = len(payload) if expected_len is None else expected_len
    flags = 0x80 if final else 0
    if recorded is None and data_outcome != "unreadable":
        recorded = crc16_xmodem(payload)
    hdr = HeaderFields(FILENAME, load, EXEC, block_no, expected_len, flags, 0)
    return BlockObservation(
        capture_id=cid,
        sample_offset=10_000 * (block_no + 1),
        header_raw=header_raw(block_no, expected_len, flags),
        header=hdr if header_ok else None,
        header_crc=CrcReading("valid" if header_ok else "invalid", 0x1234),
        payload=payload,
        expected_len=expected_len,
        actual_len=len(payload),
        data_crc=CrcReading(data_outcome, recorded),
    )


def session(block1_a: BlockObservation, block1_b: BlockObservation) -> list[CaptureObservations]:
    """Two captures: block 0 intact in both (the anchor), block 2 final and
    intact in both, block 1 as given."""
    def cap(cid, middle):
        return CaptureObservations(cid, (
            obs(cid, 0, bytes(256)),
            middle,
            obs(cid, 2, TAIL, final=True),
        ))
    return [cap(CID_A, block1_a), cap(CID_B, block1_b)]


def failed(cid: str, payload: bytes, **kw) -> BlockObservation:
    kw.setdefault("recorded", TP_CRC)
    return obs(cid, FAILED_BLOCK, payload, data_outcome="invalid", **kw)


# The brief's unique-repair case (Requirement 12.1).
UNIQUE_A = xor(TEST_PAYLOAD, {23: 0x02, 128: 0x08})
UNIQUE_B = xor(TEST_PAYLOAD, {75: 0x01, 241: 0x10})


def block1(captures, options=RecoverOptions()):
    result = recover(captures, options)
    return result, result.blocks[FAILED_BLOCK]


def assert_unrecovered(block, status, reason):
    assert (block.status, block.reason) == (status, reason)
    assert block.payload is None
    assert block.supporting_capture_ids == ()
    if block.reconstruction is not None:
        assert block.reconstruction.payload is None
        assert all(c.chosen is None and c.supporting_capture_ids == ()
                   for c in block.reconstruction.choices)


# --- step l: bounded reconstruction ------------------------------------------


def test_unique_repair_through_recover():
    caps = session(failed(CID_A, UNIQUE_A), failed(CID_B, UNIQUE_B))
    result, block = block1(caps)
    assert block.status == "Reconstructed · CRC match"
    assert block.reason is None
    assert block.payload == TEST_PAYLOAD
    assert block.supporting_capture_ids == (CID_A, CID_B)
    rec = block.reconstruction
    assert (rec.candidate_count, rec.evaluated_count, rec.match_count) == (16, 16, 1)
    sources = {c.offset: c.supporting_capture_ids for c in rec.choices}
    assert sources == {23: (CID_B,), 75: (CID_A,), 128: (CID_B,), 241: (CID_A,)}
    reads = {CID_A: UNIQUE_A, CID_B: UNIQUE_B}
    for c in rec.choices:
        assert c.chosen == TEST_PAYLOAD[c.offset]
        assert all(reads[cid][c.offset] == c.chosen for cid in c.supporting_capture_ids)
        assert c.observed == ((CID_A, UNIQUE_A[c.offset]), (CID_B, UNIQUE_B[c.offset]))

    decision = export_decision(result)
    assert decision.permitted, decision.blockers
    assert decision.reconstructed_blocks == (FAILED_BLOCK,)
    assert result.program_payload() == bytes(256) + TEST_PAYLOAD + TAIL
    # Input order does not change the result.
    assert recover(list(reversed(caps))) == result


@pytest.mark.parametrize(
    ("a", "b", "reason", "counts"),
    [
        # Requirement 12.2: the same error at offset 7 in both copies.
        (xor(UNIQUE_A, {7: 0x04}), xor(UNIQUE_B, {7: 0x04}), "no_match", (16, 16, 0)),
        # Requirement 12.3: collision.
        (xor(TEST_PAYLOAD, {20: 0x01}), xor(TEST_PAYLOAD, {21: 0x10, 22: 0x21}),
         "multiple_matches", (8, 8, 2)),
        # Requirement 12.4: seven differing bytes, the original is one read.
        (TEST_PAYLOAD, xor(TEST_PAYLOAD, {o: 0x01 for o in (3, 30, 60, 90, 120, 150, 180)}),
         "cap_exceeded", (128, 0, None)),
    ],
    ids=["no_match", "multiple_matches", "cap_exceeded"],
)
def test_search_failures_are_unresolved_with_outcome(a, b, reason, counts):
    result, block = block1(session(failed(CID_A, a), failed(CID_B, b)))
    assert_unrecovered(block, "Unresolved", reason)
    rec = block.reconstruction
    assert (rec.candidate_count, rec.evaluated_count, rec.match_count) == counts
    assert rec.reason == reason
    decision = export_decision(result)
    assert not decision.permitted
    assert (FAILED_BLOCK, reason) in decision.blockers
    assert result.program_payload() is None


def test_cap_comes_from_options():
    caps = session(failed(CID_A, UNIQUE_A), failed(CID_B, UNIQUE_B))
    _, block = block1(caps, RecoverOptions(candidate_cap=15))
    assert_unrecovered(block, "Unresolved", "cap_exceeded")
    assert block.reconstruction.evaluated_count == 0


# --- step g: intact-only baseline --------------------------------------------


def test_intact_only_mode_does_not_attempt_reconstruction():
    caps = session(failed(CID_A, UNIQUE_A), failed(CID_B, UNIQUE_B))
    result, block = block1(caps, RecoverOptions(reconstruct=False))
    assert_unrecovered(block, "CRC failed", None)
    assert block.reconstruction is None  # reported as attempted: false
    assert not export_decision(result).permitted
    # Step g runs before the evidence checks.
    _, block = block1(session(failed(CID_A, UNIQUE_A), failed(CID_B, UNIQUE_B[:100],
                                                               expected_len=256)),
                      RecoverOptions(reconstruct=False))
    assert (block.status, block.reason, block.reconstruction) == ("CRC failed", None, None)


# --- steps h to k: missing or inconsistent evidence --------------------------


def test_single_valid_header_read_is_single_read():
    bad_header = failed(CID_B, UNIQUE_B, header_ok=False)
    _, block = block1(session(failed(CID_A, UNIQUE_A), bad_header))
    assert_unrecovered(block, "Unresolved", "single_read")
    assert block.reconstruction is None
    assert bad_header in block.reads


def test_truncated_read_is_not_reconstructed():
    _, block = block1(session(failed(CID_A, UNIQUE_A),
                              failed(CID_B, UNIQUE_B[:200], expected_len=256)))
    assert_unrecovered(block, "Unresolved", "truncated_payload")
    assert block.reconstruction is None


def test_unreadable_crc_is_not_reconstructed():
    _, block = block1(session(failed(CID_A, UNIQUE_A),
                              obs(CID_B, FAILED_BLOCK, UNIQUE_B, data_outcome="unreadable")))
    assert_unrecovered(block, "Unresolved", "crc_unreadable")
    assert block.reconstruction is None


def test_different_recorded_crcs_are_not_reconstructed():
    _, block = block1(session(failed(CID_A, UNIQUE_A),
                              failed(CID_B, UNIQUE_B, recorded=TP_CRC ^ 0x0001)))
    assert_unrecovered(block, "Unresolved", "crc_mismatch_between_reads")
    assert block.reconstruction is None


def test_step_precedence():
    truncated = failed(CID_B, UNIQUE_B[:200], expected_len=256)
    unreadable_a = obs(CID_A, FAILED_BLOCK, UNIQUE_A, data_outcome="unreadable")
    # i before j: truncated and unreadable together report truncation.
    _, block = block1(session(unreadable_a, truncated))
    assert block.reason == "truncated_payload"
    # j before k: an unreadable CRC is reported rather than a CRC mismatch.
    _, block = block1(session(unreadable_a, failed(CID_B, UNIQUE_B, recorded=0x0001)))
    assert block.reason == "crc_unreadable"
    # c before l: a header disagreement is a Conflict and is never searched.
    other = failed(CID_B, UNIQUE_B, load=LOAD + 1)
    _, block = block1(session(failed(CID_A, UNIQUE_A), other))
    assert (block.status, block.reason) == ("Conflict", "header_disagreement")
    assert block.reconstruction is None and block.payload is None


# --- end to end on the damaged STARMAP WAVs ----------------------------------

ROOT = Path(__file__).resolve().parents[2]
DECODER = ROOT / "build" / "decoder" / "ScanTape"
FIXTURES = ROOT / "fixtures" / "starmap"


@pytest.mark.skipif(
    not (DECODER.is_file() and os.access(DECODER, os.X_OK)),
    reason=f"patched decoder not built at {DECODER}; run scripts/build_decoder.sh",
)
def test_damaged_fixtures_end_to_end(tmp_path):
    from taperescue.decoder_adapter import decode_capture  # test-only import

    reference = json.loads((FIXTURES / "reference_payloads.json").read_text(encoding="utf-8"))
    caps = [
        decode_capture(FIXTURES / name, decoder=DECODER, workdir=tmp_path)
        for name in ("damaged_a.wav", "damaged_b.wav")
    ]

    result = recover(caps)
    assert result.final_block == 7
    for b in result.blocks:
        expected = "Reconstructed · CRC match" if b.block_no == 6 else "Intact"
        assert b.status == expected, (b.block_no, b.status, b.reason)
    rec = result.blocks[6].reconstruction
    assert (rec.candidate_count, rec.evaluated_count, rec.match_count) == (16, 16, 1)
    assert result.blocks[6].payload.hex() == reference["blocks"][6]["hex"]

    decision = export_decision(result)
    assert decision.permitted, decision.blockers
    assert decision.reconstructed_blocks == (6,)
    assert hashlib.sha256(result.program_payload()).hexdigest() == reference["program_sha256"]

    # Requirement 14.2: intact-only merge leaves block 6 unrecovered.
    baseline = recover(caps, RecoverOptions(reconstruct=False))
    assert (baseline.blocks[6].status, baseline.blocks[6].payload) == ("CRC failed", None)
    assert not export_decision(baseline).permitted
