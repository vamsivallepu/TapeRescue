"""Property 22: UEF export round trip with recorded CRCs (task 14.2).

Generated programs of 1 to 2300 bytes (sometimes an exact multiple of 256
followed by an empty final block) are split into BBC cassette blocks. The
header bytes and both CRCs are built in this file with a local bitwise
CRC-16/XMODEM, not ``taperescue.crc`` and not the reader's routine, so the
expected values do not come from any component under test.

Sessions hold one or two captures. With two captures each block is intact in
both, intact in one and bit-damaged or absent in the other, and sometimes one
block is damaged in both at disjoint offsets while both reads record the
original CRC, so the engine must reconstruct it. Each capture has its own
next-file address, so the two captures' header bytes and header CRCs differ
and the exporter's source read is observable. Some captures also hold an
invalid-header read of a block, before its valid read, with corrupted header
bytes and random payload bytes that must never reach the image.

The positive property runs ``recover`` -> ``export_decision`` -> ``export_uef``
and reads the file back with the independent reader. It checks:

* one block in the file per generated block, in order;
* header bytes and payloads equal the generated ones; the concatenated
  payload equals the program and ``result.program_payload()``;
* the header CRC in the file equals the header CRC recorded by the
  lowest-``(capture_id, sample_offset)`` valid-header observation of that
  block, and the data CRC in the file equals the data CRC recorded by the
  block's valid-header observations (Requirement 13.5: checked against the
  captured input values, not only recomputed);
* both CRCs also recompute from the file bytes (reader flags and a local CRC);
* an empty final block has no data CRC in the file.

The negative property tampers the recorded header CRC or data CRC of the
observation the exporter takes that CRC from, keeping its outcome ``valid``.
The gate does not look at CRC values, so export is permitted; the image built
by the exporter carries the tampered value (it writes recorded CRCs and never
recomputes them), and ``export_uef`` raises ``ExportVerificationFailed`` naming
the block and leaves no file behind.

**Validates: Requirements 11.3, 11.4, 13.5**
"""

# Feature: tape-rescue, Property 22: UEF export round trip with recorded CRCs

from __future__ import annotations

import dataclasses
import os
import random
import string
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st

from taperescue.engine import recover
from taperescue.engine.gate import export_decision
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields
from taperescue.uef.exporter import build_uef, export_uef
from taperescue.uef.reader import parse_uef, read_uef
from taperescue.uef.verify import ExportVerificationFailed

FULL = 256
FINAL_FLAG, EMPTY_FLAG = 0x80, 0x40
RECONSTRUCTED = "Reconstructed · CRC match"
OUT_NAME = "program.uef"

SETTINGS = settings(
    max_examples=300,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _crc(data: bytes) -> int:
    """CRC-16/XMODEM, bit by bit: poly 0x1021, init 0, no reflection, no xorout."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _header_raw(h: HeaderFields) -> bytes:
    return (
        h.filename.encode("ascii") + b"\x00"
        + h.load_addr.to_bytes(4, "little") + h.exec_addr.to_bytes(4, "little")
        + h.block_no.to_bytes(2, "little") + h.block_len.to_bytes(2, "little")
        + bytes([h.flags]) + h.next_addr.to_bytes(4, "little")
    )


def _xor(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


# --- strategies ---------------------------------------------------------------

capture_ids = st.integers(0, 2**256 - 1).map(lambda v: f"{v:064x}")
filenames = st.text(alphabet=string.ascii_uppercase + string.digits + "!$-_.", min_size=1, max_size=10)
u32 = st.integers(0, 0xFFFF_FFFF)
bit_masks = st.sampled_from([1 << b for b in range(8)])


def _flips(lo: int, hi: int) -> st.SearchStrategy[dict[int, int]]:
    """One to three single-bit flips at distinct offsets in ``lo..hi-1``."""
    return st.dictionaries(st.integers(lo, hi - 1), bit_masks, min_size=1, max_size=min(3, hi - lo))


@dataclass(frozen=True)
class Session:
    captures: tuple[CaptureObservations, ...]
    payloads: tuple[bytes, ...]  # original payload of each block, in order
    target: int | None           # block damaged in both captures, if any


@st.composite
def sessions(draw) -> Session:
    # Block count first (1..9), so long programs are as common as short ones.
    blocks = draw(st.integers(1, 9))
    lengths = st.integers((blocks - 1) * FULL + 1, min(blocks * FULL, 2300))
    length = draw(st.one_of(lengths, st.just(blocks * FULL) if blocks < 9 else lengths))
    rng = random.Random(draw(st.integers(0, 2**32 - 1)))
    program = rng.randbytes(length)
    payloads = [program[i:i + FULL] for i in range(0, length, FULL)]
    if length % FULL == 0 and draw(st.booleans()):
        payloads.append(b"")  # empty final block after a full one
    k = len(payloads)

    filename = draw(filenames)
    load, exec_ = draw(u32), draw(u32)
    locked = draw(st.sampled_from([0x00, 0x01]))
    two = draw(st.integers(0, 2)) > 0  # two captures in about 2 of 3 sessions
    cids = draw(st.lists(capture_ids, min_size=2 if two else 1, max_size=2 if two else 1, unique=True))
    next_addrs = [draw(st.one_of(st.just(0), u32)) for _ in cids]

    # Per-block variant for each capture: I intact, D bit-damaged, None absent.
    target: int | None = None
    damage: dict[tuple[int, int], dict[int, int]] = {}
    if not two:
        variants = [("I",) for _ in range(k)]
    else:
        anchor = draw(st.integers(0, k - 1))
        candidates = [n for n in range(k) if n != anchor and len(payloads[n]) >= 2]
        if candidates and draw(st.booleans()):
            target = draw(st.sampled_from(candidates))
        variants = []
        for n in range(k):
            if n == anchor:
                variants.append(("I", "I"))
            elif n == target:
                size = len(payloads[n])
                split = draw(st.integers(1, size - 1))
                damage[(0, n)] = draw(_flips(0, split))
                damage[(1, n)] = draw(_flips(split, size))
                variants.append(("D", "D"))
            else:
                options = [("I", "I"), ("I", None), (None, "I")]
                if payloads[n]:
                    options += [("I", "D"), ("D", "I")]
                pair = draw(st.sampled_from(options))
                for c, v in enumerate(pair):
                    if v == "D":
                        damage[(c, n)] = draw(_flips(0, len(payloads[n])))
                variants.append(pair)
    strays = draw(st.sets(st.tuples(st.integers(0, len(cids) - 1), st.integers(0, k - 1)), max_size=3))

    captures = []
    for c, cid in enumerate(cids):
        reads: list[BlockObservation] = []
        for n, payload in enumerate(payloads):
            variant = variants[n][c]
            if variant is None:
                continue
            flags = locked | (FINAL_FLAG if n == k - 1 else 0) | (EMPTY_FLAG if not payload else 0)
            header = HeaderFields(filename, load, exec_, n, len(payload), flags, next_addrs[c])
            raw = _header_raw(header)
            observed = payload if variant == "I" else _xor(payload, damage[(c, n)])
            data_crc = None
            if payload:
                outcome = "valid" if observed == payload else "invalid"
                data_crc = CrcReading(outcome, _crc(payload))
            base = 100_000 * (n + 1)
            reads.append(BlockObservation(
                capture_id=cid, sample_offset=base, header_raw=raw, header=header,
                header_crc=CrcReading("valid", _crc(raw)), payload=observed,
                expected_len=len(payload), actual_len=len(payload), data_crc=data_crc,
            ))
            if (c, n) in strays:
                # Header CRC fails: one bit flipped in the next-file address
                # (the raw block number is kept, so the read is placed at n).
                bad_raw = raw[:-1] + bytes([raw[-1] ^ 0x10])
                junk = rng.randbytes(len(payload))
                reads.append(BlockObservation(
                    capture_id=cid, sample_offset=base - 50_000, header_raw=bad_raw, header=None,
                    header_crc=CrcReading("invalid", _crc(raw)), payload=junk,
                    expected_len=len(payload), actual_len=len(payload),
                    data_crc=CrcReading("invalid", _crc(payload)) if payload else None,
                ))
        rng.shuffle(reads)
        captures.append(CaptureObservations(capture_id=cid, observations=tuple(reads)))

    order = draw(st.permutations(captures))
    return Session(tuple(order), tuple(payloads), target)


# --- expected values from the inputs -----------------------------------------


def _valid_reads(captures, n: int) -> list[BlockObservation]:
    """Valid-header observations of block ``n``, by (capture_id, sample_offset)."""
    return sorted(
        (
            obs
            for cap in captures
            for obs in cap.observations
            if obs.header_crc.outcome == "valid" and obs.header is not None and obs.header.block_no == n
        ),
        key=lambda r: (r.capture_id, r.sample_offset),
    )


def _data_crc_source(captures, n: int, payload: bytes) -> BlockObservation:
    """The observation the data CRC is taken from (design section 7): the first
    intact read of ``payload``, else (a reconstructed block) the first complete
    read with a readable recorded data CRC."""
    valid = _valid_reads(captures, n)
    for r in valid:
        if r.is_intact and r.payload == payload:
            return r
    return next(
        r for r in valid
        if r.data_crc is not None and r.data_crc.outcome != "unreadable" and r.payload_state == "complete"
    )


def _recover_exportable(session: Session, captures=None):
    result = recover(session.captures if captures is None else captures)
    if session.target is not None:
        block = result.blocks[session.target]
        # A CRC collision among <= 64 candidates is a correct refusal, not a round trip.
        assume(block.reason != "multiple_matches")
        assert block.status == RECONSTRUCTED, (block.status, block.reason)
    decision = export_decision(result)
    assert decision.permitted, decision.blockers
    return result, decision


def _label(session: Session) -> None:
    event(f"captures: {len(session.captures)}")
    event(f"blocks: {len(session.payloads)}")
    event(f"empty final block: {session.payloads[-1] == b''}")
    event(f"reconstructed block: {session.target is not None}")
    strays = sum(o.header_crc.outcome != "valid" for c in session.captures for o in c.observations)
    event(f"invalid-header reads: {strays}")


# --- properties -----------------------------------------------------------------


@SETTINGS
@given(sessions())
def test_uef_export_round_trip_with_recorded_crcs(session: Session) -> None:
    _label(session)
    result, decision = _recover_exportable(session)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / OUT_NAME
        export_uef(result, decision, out)
        assert os.listdir(tmp) == [OUT_NAME]  # no temporary file left behind
        blocks = read_uef(out)

    payloads = session.payloads
    assert len(blocks) == len(payloads)
    for n, (fb, payload) in enumerate(zip(blocks, payloads)):
        valid = _valid_reads(session.captures, n)
        source = valid[0]
        assert fb.header_raw == source.header_raw
        assert fb.header_crc_in_file == source.header_crc.recorded
        assert fb.block_no == n
        assert bool(fb.flags & FINAL_FLAG) == (n == len(payloads) - 1)
        assert fb.payload == payload

        recorded = {r.data_crc.recorded for r in valid if r.data_crc is not None}
        if payload:
            assert recorded == {_crc(payload)}  # generator sanity: every read recorded the original
            assert fb.data_crc_in_file == _data_crc_source(session.captures, n, payload).data_crc.recorded
            assert fb.data_crc_in_file in recorded
            assert fb.data_crc_in_file == _crc(fb.payload)
        else:
            assert recorded == set()
            assert fb.data_crc_in_file is None
        assert fb.header_crc_ok and fb.data_crc_ok
        assert _crc(fb.header_raw) == fb.header_crc_in_file

    program = b"".join(payloads)
    assert b"".join(fb.payload for fb in blocks) == program
    assert result.program_payload() == program


@SETTINGS
@given(sessions(), st.data())
def test_tampered_recorded_crc_fails_verification(session: Session, data) -> None:
    payloads = session.payloads
    data_blocks = [n for n, p in enumerate(payloads) if p and n != session.target]
    kind = data.draw(st.sampled_from(["header", "data"] if data_blocks else ["header"]), label="kind")
    n = data.draw(st.sampled_from(data_blocks if kind == "data" else range(len(payloads))), label="block")
    delta = data.draw(st.integers(1, 0xFFFF), label="delta")
    event(f"tampered: {kind}")

    if kind == "header":
        victim = _valid_reads(session.captures, n)[0]
        tampered = CrcReading("valid", victim.header_crc.recorded ^ delta)
        replacement = dataclasses.replace(victim, header_crc=tampered)
    else:
        victim = _data_crc_source(session.captures, n, payloads[n])
        tampered = CrcReading("valid", victim.data_crc.recorded ^ delta)
        replacement = dataclasses.replace(victim, data_crc=tampered)
    captures = tuple(
        CaptureObservations(
            capture_id=cap.capture_id,
            observations=tuple(replacement if o is victim else o for o in cap.observations),
        )
        for cap in session.captures
    )

    result, decision = _recover_exportable(session, captures)

    # The exporter writes the recorded value as given; it does not recompute it.
    image_block = parse_uef(build_uef(result, decision))[n]
    in_image = image_block.header_crc_in_file if kind == "header" else image_block.data_crc_in_file
    assert in_image == tampered.recorded

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / OUT_NAME
        with pytest.raises(ExportVerificationFailed) as excinfo:
            export_uef(result, decision, out)
        assert os.listdir(tmp) == []  # neither the output nor a temporary file remains
    assert any(p.startswith(f"block {n}:") for p in excinfo.value.problems), excinfo.value.problems


def test_generator_covers_the_cases(fixed_constant_pool) -> None:
    """Fixed sample of the generator: every scenario the property relies on occurs.
    ``fixed_constant_pool`` (conftest.py) keeps the sample the same whichever
    other test modules pytest loaded."""
    seen: list[Session] = []

    @settings(max_examples=200, derandomize=True, deadline=None, database=None,
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
    @given(sessions())
    def collect(session: Session) -> None:
        seen.append(session)

    collect()
    two = [s for s in seen if len(s.captures) == 2]
    assert len(two) >= 40
    assert sum(len(s.captures) == 1 for s in seen) >= 40
    assert sum(s.target is not None for s in seen) >= 20
    assert sum(s.payloads[-1] == b"" for s in seen) >= 10
    assert sum(len(s.payloads) >= 8 for s in seen) >= 10
    assert sum(
        any(o.header_crc.outcome != "valid" for c in s.captures for o in c.observations) for s in seen
    ) >= 20
    # Header bytes differ between the two captures, so the source read matters.
    assert sum(
        len({o.header_raw for c in s.captures for o in c.observations
             if o.header is not None and o.header.block_no == 0}) == 2
        for s in two
    ) >= 20
