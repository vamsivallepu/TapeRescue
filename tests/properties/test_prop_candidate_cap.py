"""Property 13: Candidate count and cap (task 12.3).

Feature: tape-rescue, Property 13: Candidate count and cap

For any set of equal-length reads with the same recorded CRC,
``candidate_count`` equals the product of distinct observed values across
differing offsets. When that count exceeds the cap, the result is
``Unresolved`` with ``cap_exceeded``, ``evaluated_count`` is 0, no CRC is
computed, and the block is never ``Reconstructed · CRC match``, even when a
matching candidate exists. At or below the cap every candidate is evaluated.

**Validates: Requirements 9.2, 9.3, 12.4, 13.3**
"""

# Feature: tape-rescue, Property 13: Candidate count and cap

from __future__ import annotations

import math
from dataclasses import dataclass
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import taperescue.engine.reconstruct as rec
from taperescue.crc import crc16_xmodem
from taperescue.engine import RecoverOptions, recover
from taperescue.engine.reconstruct import candidate_count, reconstruct
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields

SETTINGS = settings(
    max_examples=200,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023
BLOCK = 1  # block under test; block 0 is the anchor, block 2 the final block


@dataclass(frozen=True)
class Case:
    reads: tuple[tuple[str, bytes], ...]   # (capture_id, payload)
    distinct: dict[int, int]                # differing offset -> distinct value count
    recorded_crc: int
    crc_from_candidate: bool                # recorded CRC is the CRC of some candidate
    cap: int

    @property
    def expected_count(self) -> int:
        return math.prod(self.distinct.values())


capture_ids = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


@st.composite
def cases(draw) -> Case:
    """2-3 equal-length reads that differ at a random set of offsets.

    At each differing offset, ``k`` distinct values (2 <= k <= number of reads)
    are spread over the reads so that every one of the ``k`` values occurs.
    The expected candidate count is therefore known by construction, without
    looking at the reads again.
    """
    n = draw(st.integers(2, 3))
    cids = draw(st.lists(capture_ids, min_size=n, max_size=n, unique=True))
    length = draw(st.integers(1, 48))
    base = draw(st.binary(min_size=length, max_size=length))
    offsets = draw(
        st.lists(st.integers(0, length - 1), max_size=min(length, 8), unique=True)
    )

    rows = [bytearray(base) for _ in range(n)]
    distinct: dict[int, int] = {}
    for offset in offsets:
        k = draw(st.integers(2, n))
        values = draw(st.lists(st.integers(0, 255), min_size=k, max_size=k, unique=True))
        assigned = values + [draw(st.sampled_from(values)) for _ in range(n - k)]
        assigned = draw(st.permutations(assigned))
        for row, value in zip(rows, assigned):
            row[offset] = value
        distinct[offset] = k

    # Either the CRC of one candidate (so a match exists) or an arbitrary value.
    crc_from_candidate = draw(st.booleans())
    if crc_from_candidate:
        candidate = bytearray(base)
        for offset in offsets:
            candidate[offset] = rows[draw(st.integers(0, n - 1))][offset]
        recorded = crc16_xmodem(bytes(candidate))
    else:
        recorded = draw(st.integers(0, 0xFFFF))

    count = math.prod(distinct.values())
    cap_choices = [st.just(64), st.just(count), st.integers(0, 256)]
    if count > 0:
        cap_choices.append(st.just(count - 1))
    cap = draw(st.one_of(*cap_choices))

    return Case(
        reads=tuple(zip(cids, (bytes(r) for r in rows))),
        distinct=distinct,
        recorded_crc=recorded,
        crc_from_candidate=crc_from_candidate,
        cap=cap,
    )


def counting_crc():
    """Patch the CRC used by the reconstruction module with a counting wrapper."""
    return mock.patch.object(rec, "crc16_xmodem", wraps=rec.crc16_xmodem)


@SETTINGS
@given(cases())
def test_candidate_count_and_cap(case: Case) -> None:
    """Feature: tape-rescue, Property 13: Candidate count and cap"""
    payloads = [p for _, p in case.reads]
    assert candidate_count(payloads) == case.expected_count

    with counting_crc() as crc:
        out = reconstruct(list(case.reads), case.recorded_crc, case.cap)

    assert out.candidate_count == case.expected_count
    assert {c.offset for c in out.choices} == set(case.distinct)

    if case.expected_count > case.cap:
        assert out.reason == "cap_exceeded"
        assert out.evaluated_count == 0
        assert out.match_count is None
        assert out.payload is None
        assert not out.reconstructed
        assert crc.call_count == 0
        assert all(c.chosen is None and c.supporting_capture_ids == () for c in out.choices)
    else:
        # The search is never cut short: every candidate is built and checked.
        assert out.evaluated_count == case.expected_count
        assert crc.call_count == case.expected_count
        assert out.match_count is not None
        if case.crc_from_candidate:
            assert out.match_count >= 1
        # A unique claim is made if and only if exactly one candidate matched.
        assert (out.payload is not None) == (out.match_count == 1)
        assert out.reconstructed == (out.match_count == 1)
        expected_reason = {0: "no_match", 1: None}.get(out.match_count, "multiple_matches")
        assert out.reason == expected_reason


# --- the same property through recover() -----------------------------------


def _header_raw(block_no: int, block_len: int, flags: int) -> bytes:
    return (
        FILENAME.encode() + b"\x00"
        + LOAD.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + block_len.to_bytes(2, "little")
        + bytes([flags]) + (0).to_bytes(4, "little")
    )


def _obs(cid: str, block_no: int, payload: bytes, recorded: int, final: bool = False):
    flags = 0x80 if final else 0
    outcome = "valid" if crc16_xmodem(payload) == recorded else "invalid"
    return BlockObservation(
        capture_id=cid,
        sample_offset=10_000 * (block_no + 1),
        header_raw=_header_raw(block_no, len(payload), flags),
        header=HeaderFields(FILENAME, LOAD, EXEC, block_no, len(payload), flags, 0),
        header_crc=CrcReading("valid", 0x1234),
        payload=payload,
        expected_len=len(payload),
        actual_len=len(payload),
        data_crc=CrcReading(outcome, recorded),
    )


def _session(case: Case) -> list[CaptureObservations]:
    anchor, tail = bytes(256), bytes(range(40))
    return [
        CaptureObservations(cid, (
            _obs(cid, 0, anchor, crc16_xmodem(anchor)),
            _obs(cid, BLOCK, payload, case.recorded_crc),
            _obs(cid, 2, tail, crc16_xmodem(tail), final=True),
        ))
        for cid, payload in case.reads
    ]


@SETTINGS
@given(cases())
def test_cap_exceeded_through_recover(case: Case) -> None:
    """Feature: tape-rescue, Property 13: Candidate count and cap (via recover)"""
    any_intact = any(crc16_xmodem(p) == case.recorded_crc for _, p in case.reads)

    with counting_crc() as crc:
        result = recover(_session(case), RecoverOptions(candidate_cap=case.cap))
    block = result.blocks[BLOCK]

    if any_intact:
        # Step e decides before reconstruction is reached.
        assert block.status in ("Intact", "Conflict")
        return

    assert block.reconstruction is not None
    assert block.reconstruction.candidate_count == case.expected_count
    if case.expected_count > case.cap:
        assert (block.status, block.reason) == ("Unresolved", "cap_exceeded")
        assert block.status != "Reconstructed · CRC match"
        assert block.payload is None
        assert block.supporting_capture_ids == ()
        assert block.reconstruction.evaluated_count == 0
        assert block.reconstruction.match_count is None
        assert crc.call_count == 0
    else:
        assert block.reconstruction.evaluated_count == case.expected_count
        assert crc.call_count == case.expected_count
        if block.status == "Reconstructed · CRC match":
            assert block.reconstruction.match_count == 1
            assert block.payload is not None
        else:
            assert block.status == "Unresolved"
            assert block.payload is None
