"""Byte-array acceptance tests: the brief's fixed cases (task 15).

Every case runs through ``recover()`` on full ``CaptureObservations`` and is
checked on the JSON recovery report (``build_report`` -> ``dumps_report`` ->
``loads_report``), not only on ``reconstruct()``.

Session layout for the reconstruction and missing-evidence cases: two
captures A and B, block 0 intact and equal in both (the anchor), block 1 the
block under test, block 2 the final block, intact in both. The intact
sourcing case uses blocks 0 to 7 with block 7 final.

Each read's data CRC outcome is computed from its own bytes: ``valid`` when
the CRC of the observed payload equals the recorded CRC, otherwise
``invalid``. So "both reads fail" is a property of the fixture bytes, not a
label set by hand.

Requirements 8.5, 9.13, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6.
"""

from __future__ import annotations

import hashlib
import math

from taperescue.crc import crc16_xmodem
from taperescue.engine import RecoverOptions, recover
from taperescue.engine.report import (
    RECONSTRUCTED_LABEL,
    STATUS_LABELS,
    build_report,
    dumps_report,
    loads_report,
    reason_message,
)
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields

CID_A = "a" * 64
CID_B = "b" * 64
FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023

TEST_PAYLOAD = bytes((17 * i + 11 * i * i + 42) % 256 for i in range(256))
TP_CRC = 0xAC93
BLOCK = 1  # the block under test in the three-block session
ANCHOR = bytes(range(256))
TAIL = bytes(range(40))  # payload of final block 2


# --- traceability: the CRC vectors the cases depend on (Requirement 6) ------


def test_crc_vectors():
    assert crc16_xmodem(b"123456789") == 0x31C3
    assert len(TEST_PAYLOAD) == 256
    assert crc16_xmodem(TEST_PAYLOAD) == TP_CRC == 0xAC93


# --- fixture helpers ----------------------------------------------------------


def xor(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


def header_raw(block_no: int, block_len: int, flags: int, load: int) -> bytes:
    return (
        FILENAME.encode() + b"\x00"
        + load.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + block_len.to_bytes(2, "little")
        + bytes([flags]) + (0).to_bytes(4, "little")
    )


def read(
    cid: str,
    block_no: int,
    payload: bytes,
    *,
    recorded: int | None = None,
    crc_unreadable: bool = False,
    expected_len: int | None = None,
    final: bool = False,
    load: int = LOAD,
) -> BlockObservation:
    """One read with a valid header. ``recorded`` defaults to the CRC of
    ``payload`` (an intact read); the data CRC outcome follows from the bytes."""
    expected_len = len(payload) if expected_len is None else expected_len
    flags = 0x80 if final else 0
    if crc_unreadable:
        data_crc = CrcReading("unreadable", None)
    else:
        recorded = crc16_xmodem(payload) if recorded is None else recorded
        ok = len(payload) == expected_len and crc16_xmodem(payload) == recorded
        data_crc = CrcReading("valid" if ok else "invalid", recorded)
    return BlockObservation(
        capture_id=cid,
        sample_offset=10_000 * (block_no + 1),
        header_raw=header_raw(block_no, expected_len, flags, load),
        header=HeaderFields(FILENAME, load, EXEC, block_no, expected_len, flags, 0),
        header_crc=CrcReading("valid", 0x1234),
        payload=payload,
        expected_len=expected_len,
        actual_len=len(payload),
        data_crc=data_crc,
    )


def session(block_a: BlockObservation, block_b: BlockObservation) -> list[CaptureObservations]:
    def cap(cid, middle):
        return CaptureObservations(cid, (
            read(cid, 0, ANCHOR),
            middle,
            read(cid, 2, TAIL, final=True),
        ))
    return [cap(CID_A, block_a), cap(CID_B, block_b)]


def failing_pair(a: bytes, b: bytes) -> list[CaptureObservations]:
    """Block 1 read from A and from B, both recording the Test_Payload's CRC."""
    return session(read(CID_A, BLOCK, a, recorded=TP_CRC), read(CID_B, BLOCK, b, recorded=TP_CRC))


def report_for(captures, options=RecoverOptions()) -> dict:
    """The report as JSON text parsed back, so assertions see what a user downloads."""
    report = loads_report(dumps_report(build_report(recover(captures, options))))
    # Input order never changes the report text (Requirement 10.6).
    reversed_report = build_report(recover(list(reversed(captures)), options))
    assert dumps_report(reversed_report) == dumps_report(report)
    return report


def entry(report: dict, block_no: int) -> dict:
    [found] = [b for b in report["blocks"] if b["block_no"] == block_no]
    return found


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recorded_crcs_in_inputs(captures) -> set[int]:
    return {
        o.data_crc.recorded
        for c in captures for o in c.observations
        if o.data_crc is not None and o.data_crc.recorded is not None
    }


def all_recorded_crc_values(node) -> list:
    """Every value under a ``recorded_data_crc`` key anywhere in the report."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "recorded_data_crc":
                found.append(v)
            found.extend(all_recorded_crc_values(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(all_recorded_crc_values(v))
    return found


def assert_no_filled_in(report: dict, captures, block_no: int) -> None:
    """An unrecovered block carries no chosen bytes, no payload digest, no
    supporting captures and no substitute CRC, and the session exposes no
    recovered program digest."""
    e = entry(report, block_no)
    assert e["payload_sha256"] is None
    assert e["supporting_capture_ids"] == []
    for c in e["reconstruction"]["choices"]:
        assert c["chosen"] is None
        assert c["supporting_capture_ids"] == []
    # Every read digest is the digest of bytes actually observed (never padded).
    observed = {
        (o.capture_id, o.sample_offset): o
        for c in captures for o in c.observations
    }
    for r in e["reads"]:
        o = observed[(r["capture_id"], r["sample_offset"])]
        assert r["actual_len"] == len(o.payload)
        assert r["payload_sha256"] == sha(o.payload)
        assert r["recorded_data_crc"] == (o.data_crc.recorded if o.data_crc else None)
    # No CRC value in the whole report was computed rather than read from tape.
    tape_crcs = recorded_crcs_in_inputs(captures)
    for v in all_recorded_crc_values(report):
        assert v is None or v in tape_crcs
    assert "recovered_payload_sha256" not in report
    assert report["export"]["permitted"] is False
    assert block_no in {n for n, _ in report["export"]["blockers"]}


# --- Requirement 12.1: shared failure with a unique repair --------------------

UNIQUE_A = xor(TEST_PAYLOAD, {23: 0x02, 128: 0x08})
UNIQUE_B = xor(TEST_PAYLOAD, {75: 0x01, 241: 0x10})


def test_unique_repair_reconstructs_the_test_payload():
    caps = failing_pair(UNIQUE_A, UNIQUE_B)
    result = recover(caps)
    report = report_for(caps)
    e = entry(report, BLOCK)

    # Both reads fail their data CRC and both recorded 0xAC93.
    assert [(r["capture_id"], r["data_crc"], r["recorded_data_crc"]) for r in e["reads"]] == [
        (CID_A, "invalid", TP_CRC),
        (CID_B, "invalid", TP_CRC),
    ]
    assert e["status"] == "Reconstructed · CRC match"
    assert e["reason"] is None
    assert e["recorded_data_crc"] == TP_CRC
    rec = e["reconstruction"]
    assert rec["attempted"] is True
    assert (rec["candidate_count"], rec["evaluated_count"], rec["match_count"]) == (16, 16, 1)

    # The single match equals the Test_Payload byte for byte.
    assert result.blocks[BLOCK].payload == TEST_PAYLOAD
    assert e["payload_sha256"] == sha(TEST_PAYLOAD)
    assert e["supporting_capture_ids"] == [CID_A, CID_B]

    # A supplies offsets 75 and 241, B supplies 23 and 128.
    assert rec["choices"] == [
        {"offset": 23, "observed": {CID_A: UNIQUE_A[23], CID_B: TEST_PAYLOAD[23]},
         "chosen": TEST_PAYLOAD[23], "supporting_capture_ids": [CID_B]},
        {"offset": 75, "observed": {CID_A: TEST_PAYLOAD[75], CID_B: UNIQUE_B[75]},
         "chosen": TEST_PAYLOAD[75], "supporting_capture_ids": [CID_A]},
        {"offset": 128, "observed": {CID_A: UNIQUE_A[128], CID_B: TEST_PAYLOAD[128]},
         "chosen": TEST_PAYLOAD[128], "supporting_capture_ids": [CID_B]},
        {"offset": 241, "observed": {CID_A: TEST_PAYLOAD[241], CID_B: UNIQUE_B[241]},
         "chosen": TEST_PAYLOAD[241], "supporting_capture_ids": [CID_A]},
    ]

    assert report["export"]["permitted"] is True
    assert report["export"]["reconstructed_blocks"] == [BLOCK]
    assert report["recovered_payload_sha256"] == sha(ANCHOR + TEST_PAYLOAD + TAIL)


def test_reconstructed_label_differs_from_intact():
    """Requirement 9.13: a Reconstructed block is labelled as a CRC match."""
    report = report_for(failing_pair(UNIQUE_A, UNIQUE_B))
    reconstructed, intact = entry(report, BLOCK), entry(report, 0)
    assert intact["status"] == "Intact"
    assert intact["label"] == STATUS_LABELS["Intact"]
    assert reconstructed["label"] == RECONSTRUCTED_LABEL
    assert "CRC match" in reconstructed["label"]
    assert reconstructed["label"] != intact["label"]
    assert reconstructed["status"] != intact["status"]


# --- Requirements 12.2, 12.3, 12.4: search outcomes left Unresolved -----------


def test_same_error_in_both_reads_has_no_match():
    a, b = xor(UNIQUE_A, {7: 0x04}), xor(UNIQUE_B, {7: 0x04})
    caps = failing_pair(a, b)
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert [r["data_crc"] for r in e["reads"]] == ["invalid", "invalid"]
    assert (e["status"], e["reason"]) == ("Unresolved", "no_match")
    assert e["reason_message"] == reason_message("no_match")
    rec = e["reconstruction"]
    assert rec["attempted"] is True
    assert (rec["candidate_count"], rec["evaluated_count"], rec["match_count"]) == (16, 16, 0)
    # Offset 7 agrees in both reads, so it is not a differing offset.
    assert [c["offset"] for c in rec["choices"]] == [23, 75, 128, 241]
    assert e["recorded_data_crc"] == TP_CRC  # the value on tape, reported as read
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "no_match"] in report["export"]["blockers"]


def test_crc_collision_has_multiple_matches():
    a = xor(TEST_PAYLOAD, {20: 0x01})
    b = xor(TEST_PAYLOAD, {21: 0x10, 22: 0x21})
    caps = failing_pair(a, b)
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert [r["data_crc"] for r in e["reads"]] == ["invalid", "invalid"]
    assert (e["status"], e["reason"]) == ("Unresolved", "multiple_matches")
    rec = e["reconstruction"]
    assert (rec["candidate_count"], rec["evaluated_count"], rec["match_count"]) == (8, 8, 2)
    assert [c["offset"] for c in rec["choices"]] == [20, 21, 22]
    assert rec["choices"][0]["observed"] == {CID_A: a[20], CID_B: b[20]}
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "multiple_matches"] in report["export"]["blockers"]


CAP_A_OFFSETS = (3, 30, 60, 90)
CAP_B_OFFSETS = (120, 150, 180)


def test_seven_differing_offsets_exceed_the_cap_without_evaluation():
    a = xor(TEST_PAYLOAD, {o: 0x01 for o in CAP_A_OFFSETS})
    b = xor(TEST_PAYLOAD, {o: 0x01 for o in CAP_B_OFFSETS})
    # A matching candidate exists: at every offset the Test_Payload byte was
    # observed in one of the reads, and the Test_Payload matches the CRC.
    for o in CAP_A_OFFSETS + CAP_B_OFFSETS:
        assert TEST_PAYLOAD[o] in (a[o], b[o])
    assert crc16_xmodem(TEST_PAYLOAD) == TP_CRC
    assert math.prod([2] * 7) == 128

    caps = failing_pair(a, b)
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert [r["data_crc"] for r in e["reads"]] == ["invalid", "invalid"]
    assert (e["status"], e["reason"]) == ("Unresolved", "cap_exceeded")
    rec = e["reconstruction"]
    assert report["candidate_cap"] == 64
    assert (rec["candidate_count"], rec["evaluated_count"], rec["match_count"]) == (128, 0, None)
    assert [c["offset"] for c in rec["choices"]] == sorted(CAP_A_OFFSETS + CAP_B_OFFSETS)
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "cap_exceeded"] in report["export"]["blockers"]


# --- Requirement 12.5: missing or inconsistent evidence -----------------------


def test_truncated_read_is_unresolved_and_not_padded():
    short = UNIQUE_B[:200]
    caps = session(
        read(CID_A, BLOCK, UNIQUE_A, recorded=TP_CRC),
        read(CID_B, BLOCK, short, recorded=TP_CRC, expected_len=256),
    )
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert (e["status"], e["reason"]) == ("Unresolved", "truncated_payload")
    assert e["reconstruction"]["attempted"] is False
    [rb] = [r for r in e["reads"] if r["capture_id"] == CID_B]
    assert (rb["payload_state"], rb["expected_len"], rb["actual_len"]) == ("truncated", 256, 200)
    assert rb["payload_sha256"] == sha(short)  # the 200 bytes read, no padding
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "truncated_payload"] in report["export"]["blockers"]


def test_unreadable_crc_is_unresolved_with_no_substitute_crc():
    caps = session(
        read(CID_A, BLOCK, UNIQUE_A, recorded=TP_CRC),
        read(CID_B, BLOCK, UNIQUE_B, crc_unreadable=True),
    )
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert (e["status"], e["reason"]) == ("Unresolved", "crc_unreadable")
    assert e["reconstruction"]["attempted"] is False
    assert e["recorded_data_crc"] is None
    [rb] = [r for r in e["reads"] if r["capture_id"] == CID_B]
    assert (rb["data_crc"], rb["recorded_data_crc"]) == ("unreadable", None)
    # In particular, the CRC of B's bytes was not computed and filled in.
    assert crc16_xmodem(UNIQUE_B) not in all_recorded_crc_values(report)
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "crc_unreadable"] in report["export"]["blockers"]


def test_incompatible_headers_are_a_conflict_and_not_reconstructed():
    caps = session(
        read(CID_A, BLOCK, UNIQUE_A, recorded=TP_CRC),
        read(CID_B, BLOCK, UNIQUE_B, recorded=TP_CRC, load=LOAD + 1),
    )
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert (e["status"], e["reason"]) == ("Conflict", "header_disagreement")
    assert e["conflict"] == {"kind": "header", "capture_ids": [CID_A, CID_B]}
    assert e["reconstruction"]["attempted"] is False
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "header_disagreement"] in report["export"]["blockers"]


def test_conflicting_intact_reads_are_a_conflict():
    other = xor(TEST_PAYLOAD, {75: 0x01})
    caps = session(read(CID_A, BLOCK, TEST_PAYLOAD), read(CID_B, BLOCK, other))
    report = report_for(caps)
    e = entry(report, BLOCK)
    assert [r["data_crc"] for r in e["reads"]] == ["valid", "valid"]
    assert (e["status"], e["reason"]) == ("Conflict", None)
    assert e["reason_message"] == reason_message("payload_conflict")
    assert e["conflict"] == {"kind": "payload", "capture_ids": [CID_A, CID_B]}
    assert e["reconstruction"]["attempted"] is False
    # The reads disagree on the recorded CRC, so the block reports none.
    assert e["recorded_data_crc"] is None
    assert_no_filled_in(report, caps, BLOCK)
    assert [BLOCK, "payload_conflict"] in report["export"]["blockers"]


# --- Requirements 8.5, 12.6: intact block selection across captures ----------


def program_blocks() -> list[bytes]:
    return [bytes((n * 31 + i) % 256 for i in range(256)) for n in range(8)]


def test_intact_blocks_are_sourced_from_the_capture_that_holds_them():
    blocks = program_blocks()

    def cap(cid: str, bad: int) -> CaptureObservations:
        reads = []
        for n, payload in enumerate(blocks):
            final = n == 7
            if n == bad:
                damaged = xor(payload, {10: 0x40})
                reads.append(read(cid, n, damaged, recorded=crc16_xmodem(payload), final=final))
            else:
                reads.append(read(cid, n, payload, final=final))
        return CaptureObservations(cid, tuple(reads))

    caps = [cap(CID_A, bad=6), cap(CID_B, bad=4)]
    report = report_for(caps)

    b4, b6 = entry(report, 4), entry(report, 6)
    assert {r["capture_id"]: r["data_crc"] for r in b4["reads"]} == {CID_A: "valid", CID_B: "invalid"}
    assert {r["capture_id"]: r["data_crc"] for r in b6["reads"]} == {CID_A: "invalid", CID_B: "valid"}
    assert (b4["status"], b4["supporting_capture_ids"]) == ("Intact", [CID_A])
    assert (b6["status"], b6["supporting_capture_ids"]) == ("Intact", [CID_B])
    assert b4["payload_sha256"] == sha(blocks[4])
    assert b6["payload_sha256"] == sha(blocks[6])
    for e in (b4, b6):
        assert e["reason"] is None
        assert e["label"] == STATUS_LABELS["Intact"]
        assert e["reconstruction"]["attempted"] is False

    for n in (0, 1, 2, 3, 5, 7):
        assert entry(report, n)["supporting_capture_ids"] == [CID_A, CID_B]
    assert report["anchors"] == [0, 1, 2, 3, 5, 7]
    assert report["final_block"] == 7
    assert report["export"] == {
        "permitted": True, "blockers": [], "messages": {}, "reconstructed_blocks": [],
    }
    assert report["recovered_payload_sha256"] == sha(b"".join(blocks))
