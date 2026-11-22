"""Byte-array cases for bounded reconstruction (task 12.1).

Uses the brief's Test_Payload ``payload[i] = (17*i + 11*i*i + 42) % 256``,
whose CRC-16/XMODEM is 0xAC93.

Requirements: 9.2, 9.3, 9.4, 9.5, 9.6, 9.7
"""

from __future__ import annotations

import pytest

import taperescue.engine.reconstruct as rec
from taperescue.crc import crc16_xmodem
from taperescue.engine.reconstruct import candidate_count, differing_offsets, reconstruct

CAP = 64
CRC = 0xAC93
A = "a" * 64
B = "b" * 64


def make_test_payload() -> bytes:
    return bytes((17 * i + 11 * i * i + 42) % 256 for i in range(256))


PAYLOAD = make_test_payload()


def damaged(flips: dict[int, int], base: bytes = PAYLOAD) -> bytes:
    out = bytearray(base)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


def test_test_payload_crc() -> None:
    assert crc16_xmodem(PAYLOAD) == CRC


# --- Requirement 12.1: unique repair -------------------------------------------------


def unique_repair_reads() -> list[tuple[str, bytes]]:
    a = damaged({23: 0x02, 128: 0x08})
    b = damaged({75: 0x01, 241: 0x10})
    return [(A, a), (B, b)]


def test_unique_repair() -> None:
    reads = unique_repair_reads()
    for _, p in reads:
        assert crc16_xmodem(p) != CRC  # both reads fail on their own

    out = reconstruct(reads, CRC, CAP)

    assert out.candidate_count == 16
    assert out.evaluated_count == 16
    assert out.match_count == 1
    assert out.reason is None
    assert out.payload == PAYLOAD
    assert out.reconstructed

    by_offset = {c.offset: c for c in out.choices}
    assert sorted(by_offset) == [23, 75, 128, 241]
    for offset in (75, 241):
        assert by_offset[offset].supporting_capture_ids == (A,)
    for offset in (23, 128):
        assert by_offset[offset].supporting_capture_ids == (B,)
    for c in out.choices:
        assert c.chosen == PAYLOAD[c.offset]
        assert dict(c.observed) == {A: reads[0][1][c.offset], B: reads[1][1][c.offset]}


def test_unique_repair_ignores_input_order() -> None:
    reads = unique_repair_reads()
    assert reconstruct(reads, CRC, CAP) == reconstruct(list(reversed(reads)), CRC, CAP)


# --- Requirement 12.2: same error in both copies -------------------------------------


def test_shared_error_gives_no_match() -> None:
    a = damaged({7: 0x04, 23: 0x02, 128: 0x08})
    b = damaged({7: 0x04, 75: 0x01, 241: 0x10})

    out = reconstruct([(A, a), (B, b)], CRC, CAP)

    assert out.candidate_count == 16
    assert out.evaluated_count == 16
    assert out.match_count == 0
    assert out.reason == "no_match"
    assert out.payload is None
    assert 7 not in {c.offset for c in out.choices}  # agreed, so kept as observed
    assert all(c.chosen is None and c.supporting_capture_ids == () for c in out.choices)


# --- Requirement 12.3: collision -------------------------------------------------------


def test_collision_gives_multiple_matches() -> None:
    a = damaged({20: 0x01})
    b = damaged({21: 0x10, 22: 0x21})
    assert crc16_xmodem(a) != CRC and crc16_xmodem(b) != CRC

    out = reconstruct([(A, a), (B, b)], CRC, CAP)

    assert out.candidate_count == 8
    assert out.evaluated_count == 8
    assert out.match_count == 2
    assert out.reason == "multiple_matches"
    assert out.payload is None
    assert all(c.chosen is None for c in out.choices)


# --- Requirement 12.4: cap exceeded ----------------------------------------------------


def test_cap_exceeded_evaluates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    offsets = [3, 40, 77, 110, 150, 199, 250]
    b = damaged({o: 0x01 for o in offsets})
    reads = [(A, PAYLOAD), (B, b)]  # the original is one of the candidates

    calls = []
    real = rec.crc16_xmodem
    monkeypatch.setattr(rec, "crc16_xmodem", lambda data: calls.append(1) or real(data))

    out = reconstruct(reads, CRC, CAP)

    assert out.candidate_count == 128
    assert out.evaluated_count == 0
    assert out.match_count is None
    assert out.reason == "cap_exceeded"
    assert out.payload is None
    assert calls == []
    assert [c.offset for c in out.choices] == offsets


def test_cap_boundary_is_inclusive() -> None:
    offsets = [5, 50, 100, 150, 200, 250]  # 2**6 = 64 candidates
    reads = [(A, PAYLOAD), (B, damaged({o: 0x80 for o in offsets}))]

    out = reconstruct(reads, CRC, CAP)

    assert out.candidate_count == 64
    assert out.evaluated_count == 64
    assert out.match_count is not None and out.match_count >= 1


# --- candidate_count -------------------------------------------------------------------


def test_candidate_count_is_product_of_distinct_values() -> None:
    r1 = bytes([1, 2, 3, 4])
    r2 = bytes([1, 9, 3, 5])
    r3 = bytes([1, 8, 3, 5])
    assert differing_offsets([r1, r2, r3]) == ((1, (2, 8, 9)), (3, (4, 5)))
    assert candidate_count([r1, r2, r3]) == 6


def test_identical_reads_give_one_candidate() -> None:
    assert candidate_count([PAYLOAD, PAYLOAD]) == 1
    bad = damaged({0: 0x01})
    out = reconstruct([(A, bad), (B, bad)], CRC, CAP)
    assert out.candidate_count == 1
    assert out.evaluated_count == 1
    assert out.reason == "no_match"
    assert out.choices == ()


def test_unequal_lengths_rejected() -> None:
    with pytest.raises(ValueError):
        reconstruct([(A, PAYLOAD), (B, PAYLOAD[:-1])], CRC, CAP)
