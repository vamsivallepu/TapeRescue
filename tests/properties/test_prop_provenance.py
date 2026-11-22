"""Property 15: reconstructed bytes trace to observations (task 12.5).

Generated two-capture sessions go through ``recover``. Block 0 is an intact
anchor in both captures, block 2 is the intact final block, and block 1 is the
target: every valid-header read of it carries the original recorded data CRC
and planted bit damage at offsets that are usually disjoint between captures
and sometimes overlap. Capture A may hold a second valid-header re-read of
block 1, and may hold an invalid-header read placed at block 1 whose bytes
must never be used.

For every block reported as ``Reconstructed · CRC match`` the test checks,
against the generated inputs rather than the engine's own bookkeeping:

* every payload byte equals a value observed at that offset in at least one
  eligible (valid-header) read;
* offsets where all eligible reads agree keep the agreed value;
* each ``OffsetChoice`` lists exactly the eligible observations at a differing
  offset, its ``chosen`` value is among them and is the payload byte, and its
  ``supporting_capture_ids`` are exactly the captures that observed it there;
* ``crc16_xmodem(payload)`` equals the recorded data CRC.

**Validates: Requirements 9.5, 10.4, 13.2**
"""

# Feature: tape-rescue, Property 15: Reconstructed bytes trace to observations

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from taperescue.crc import crc16_xmodem
from taperescue.engine import recover
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields

FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023
TARGET = 1
RECONSTRUCTED = "Reconstructed · CRC match"

SETTINGS = settings(
    max_examples=300,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# --- observation builders -----------------------------------------------------


def _header_raw(block_no: int, block_len: int, flags: int) -> bytes:
    return (
        FILENAME.encode() + b"\x00"
        + LOAD.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + block_len.to_bytes(2, "little")
        + bytes([flags]) + (0).to_bytes(4, "little")
    )


def _read(
    cid: str,
    block_no: int,
    payload: bytes,
    recorded: int,
    sample_offset: int,
    *,
    final: bool = False,
    header_ok: bool = True,
) -> BlockObservation:
    flags = 0x80 if final else 0
    header = HeaderFields(FILENAME, LOAD, EXEC, block_no, len(payload), flags, 0)
    outcome = "valid" if crc16_xmodem(payload) == recorded else "invalid"
    return BlockObservation(
        capture_id=cid,
        sample_offset=sample_offset,
        header_raw=_header_raw(block_no, len(payload), flags),
        header=header if header_ok else None,
        header_crc=CrcReading("valid" if header_ok else "invalid", 0x1234),
        payload=payload,
        expected_len=len(payload),
        actual_len=len(payload),
        data_crc=CrcReading(outcome, recorded),
    )


def _xor(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


# --- strategies ---------------------------------------------------------------

capture_ids = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


def _flips(length: int, offsets: st.SearchStrategy[int]) -> st.SearchStrategy[dict[int, int]]:
    """One to three damaged offsets, each XORed with a single bit."""
    return st.dictionaries(
        offsets,
        st.sampled_from([1 << b for b in range(8)]),
        min_size=1,
        max_size=min(3, length),
    )


@dataclass(frozen=True)
class Session:
    captures: tuple[CaptureObservations, ...]
    original: bytes
    recorded: int
    eligible: tuple[tuple[str, bytes], ...]  # valid-header reads of TARGET


@st.composite
def sessions(draw) -> Session:
    cid_a, cid_b = draw(st.lists(capture_ids, min_size=2, max_size=2, unique=True))
    original = draw(st.binary(min_size=1, max_size=48))
    length = len(original)
    recorded = crc16_xmodem(original)
    anchor = draw(st.binary(min_size=0, max_size=32))
    tail = draw(st.binary(min_size=0, max_size=32))

    # Disjoint offsets most of the time; a shared pool sometimes lets A and B
    # damage the same offset (with the same or a different bit).
    overlap = draw(st.integers(0, 3)) == 0  # shared offset pool in about 1 in 4
    all_offsets = st.integers(0, length - 1)
    if overlap or length < 2:
        offsets_a = offsets_b = all_offsets
    else:
        split = draw(st.integers(1, length - 1))
        offsets_a = st.integers(0, split - 1)
        offsets_b = st.integers(split, length - 1)
    flips_a = draw(_flips(length, offsets_a))
    flips_b = draw(_flips(length, offsets_b))
    reread_a = draw(st.one_of(st.none(), _flips(length, all_offsets)))
    stray = draw(st.one_of(st.none(), st.binary(min_size=length, max_size=length)))

    eligible: list[tuple[str, bytes]] = []

    def capture(cid: str, flips: dict[int, int], extra: dict[int, int] | None,
                stray_payload: bytes | None) -> CaptureObservations:
        reads = [_read(cid, 0, anchor, crc16_xmodem(anchor), 1_000)]
        damaged = _xor(original, flips)
        reads.append(_read(cid, TARGET, damaged, recorded, 2_000))
        eligible.append((cid, damaged))
        if extra is not None:
            again = _xor(original, extra)
            reads.append(_read(cid, TARGET, again, recorded, 3_000))
            eligible.append((cid, again))
        if stray_payload is not None:
            reads.append(
                _read(cid, TARGET, stray_payload, recorded, 4_000, header_ok=False)
            )
        reads.append(_read(cid, 2, tail, crc16_xmodem(tail), 5_000, final=True))
        return CaptureObservations(capture_id=cid, observations=tuple(reads))

    caps = (capture(cid_a, flips_a, reread_a, stray), capture(cid_b, flips_b, None, None))
    order = draw(st.permutations(caps))
    return Session(tuple(order), original, recorded, tuple(eligible))


# --- property -----------------------------------------------------------------


def _check_provenance(block, eligible: tuple[tuple[str, bytes], ...], recorded: int) -> None:
    payload = block.payload
    assert payload is not None
    length = len(eligible[0][1])
    assert len(payload) == length
    assert crc16_xmodem(payload) == recorded

    differing = {}
    for i in range(length):
        values = {p[i] for _, p in eligible}
        assert payload[i] in values, f"offset {i}: {payload[i]} not observed in {values}"
        if len(values) == 1:
            assert payload[i] == next(iter(values))
        else:
            differing[i] = tuple(sorted((cid, p[i]) for cid, p in eligible))

    outcome = block.reconstruction
    assert outcome is not None and outcome.reason is None and outcome.match_count == 1
    assert [c.offset for c in outcome.choices] == sorted(differing)
    for choice in outcome.choices:
        observed = differing[choice.offset]
        assert choice.observed == observed
        assert choice.chosen == payload[choice.offset]
        assert choice.chosen in {v for _, v in observed}
        expected_support = tuple(sorted({cid for cid, v in observed if v == choice.chosen}))
        assert choice.supporting_capture_ids == expected_support

    assert set(block.supporting_capture_ids) <= {cid for cid, _ in eligible}


@SETTINGS
@given(sessions())
def test_reconstructed_bytes_trace_to_observations(session: Session) -> None:
    result = recover(session.captures)
    reconstructed = [b for b in result.blocks if b.status == RECONSTRUCTED]
    event(f"target status: {result.blocks[TARGET].status}")
    for block in reconstructed:
        assert block.block_no == TARGET
        _check_provenance(block, session.eligible, session.recorded)


def test_generator_reaches_reconstruction() -> None:
    """Fixed sample of the generator: the reconstruction path must be exercised,
    otherwise the property above would hold vacuously."""
    seen = []

    @settings(max_examples=200, derandomize=True, deadline=None, database=None)
    @given(sessions())
    def collect(session: Session) -> None:
        seen.append(recover(session.captures).blocks[TARGET].status)

    collect()
    assert seen.count(RECONSTRUCTED) >= 20, {s: seen.count(s) for s in set(seen)}


def test_example_unique_repair_provenance() -> None:
    """Fixed case: A damages offset 1, B damages offset 3; one candidate matches."""
    original = bytes([10, 20, 30, 40, 50])
    recorded = crc16_xmodem(original)
    cid_a, cid_b = "a" * 64, "b" * 64
    a = _xor(original, {1: 0x04})
    b = _xor(original, {3: 0x40})

    def cap(cid: str, damaged: bytes) -> CaptureObservations:
        return CaptureObservations(cid, (
            _read(cid, 0, b"anchor", crc16_xmodem(b"anchor"), 1_000),
            _read(cid, TARGET, damaged, recorded, 2_000),
            _read(cid, 2, b"end", crc16_xmodem(b"end"), 3_000, final=True),
        ))

    result = recover([cap(cid_b, b), cap(cid_a, a)])
    block = result.blocks[TARGET]
    assert block.status == RECONSTRUCTED
    assert block.payload == original
    _check_provenance(block, ((cid_a, a), (cid_b, b)), recorded)
    by_offset = {c.offset: c for c in block.reconstruction.choices}
    assert by_offset[1].supporting_capture_ids == (cid_b,)
    assert by_offset[3].supporting_capture_ids == (cid_a,)
