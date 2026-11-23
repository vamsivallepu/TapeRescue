"""Property 12: input order does not change results (task 13.2).

Generated sessions of one or two captures (plus, sometimes, a duplicate copy
of a capture) go through ``recover`` twice: once as generated, once with the
capture list permuted and every capture's observation tuple permuted. The two
runs must give:

* equal ``RecoveryResult`` objects;
* equal ``export_decision`` values;
* byte-identical ``dumps_report(build_report(...))`` text;
* equal ``program_payload()``;
* or, when the session has no anchor, ``PairingRejected("no_anchor")`` in both.

Each block of each capture gets one read drawn from a mix of kinds: intact,
damaged with the original recorded CRC (reconstructable when the damage is
disjoint), truncated, unreadable data CRC, invalid or unreadable header CRC,
absent, intact with different bytes (payload conflict), header disagreement,
and a different recorded CRC. Captures may hold re-reads of a block, some at
the same sample offset as the first read, so ordering ties are exercised.
Non-final blocks hold 256 bytes so some sessions pass the export gate.

**Validates: Requirements 8.3, 10.6, 13.1**
"""

# Feature: tape-rescue, Property 12: Input order does not change results

from __future__ import annotations

import hashlib
from collections import Counter

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from taperescue.crc import crc16_xmodem
from taperescue.engine import PairingRejected, recover
from taperescue.engine.gate import export_decision
from taperescue.engine.report import build_report, dumps_report
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields

FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023
FINAL_FLAG = 0x80
RECONSTRUCTED = "Reconstructed · CRC match"

SETTINGS = settings(
    max_examples=300,
    derandomize=True,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# Weighted so intact and damaged reads dominate and reconstruction is reached.
READ_KINDS = (
    ["intact"] * 4
    + ["damaged"] * 4
    + [
        "truncated",
        "crc_unreadable",
        "header_invalid",
        "header_unreadable",
        "absent",
        "other_payload",
        "header_disagree",
        "crc_other",
    ]
)


# --- observation builders -----------------------------------------------------


def _expand(seed: bytes, block_no: int, length: int) -> bytes:
    """Deterministic pseudo-random payload bytes from a short drawn seed."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + bytes([block_no, counter])).digest()
        counter += 1
    return bytes(out[:length])


def _header_raw(block_no: int, block_len: int, flags: int, load: int) -> bytes:
    return (
        FILENAME.encode() + b"\x00"
        + load.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
        + block_no.to_bytes(2, "little") + block_len.to_bytes(2, "little")
        + bytes([flags]) + (0).to_bytes(4, "little")
    )


def _obs(
    cid: str,
    block_no: int,
    payload: bytes,
    sample_offset: int,
    *,
    expected_len: int | None = None,
    data_crc: CrcReading | None,
    flags: int = 0,
    load: int = LOAD,
    header_outcome: str = "valid",
    raw_block_no: int | None = None,
) -> BlockObservation:
    expected = len(payload) if expected_len is None else expected_len
    header = HeaderFields(FILENAME, load, EXEC, block_no, expected, flags, 0)
    raw = _header_raw(block_no if raw_block_no is None else raw_block_no, expected, flags, load)
    return BlockObservation(
        capture_id=cid,
        sample_offset=sample_offset,
        header_raw=raw,
        header=header if header_outcome == "valid" else None,
        header_crc=CrcReading(header_outcome, None if header_outcome == "unreadable" else 0x1234),
        payload=payload,
        expected_len=expected,
        actual_len=len(payload),
        data_crc=data_crc,
    )


def _crc_reading(payload: bytes, recorded: int) -> CrcReading:
    return CrcReading("valid" if crc16_xmodem(payload) == recorded else "invalid", recorded)


def _xor(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


# --- strategies ---------------------------------------------------------------

capture_ids = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


@st.composite
def _flips(draw, length: int) -> dict[int, int]:
    """One to three damaged offsets, each XORed with a single bit."""
    return draw(
        st.dictionaries(
            st.integers(0, length - 1),
            st.sampled_from([1 << b for b in range(8)]),
            min_size=1,
            max_size=min(3, length),
        )
    )


@st.composite
def _read_of_kind(draw, kind: str, cid: str, block_no: int, original: bytes,
                  flags: int, sample_offset: int) -> BlockObservation | None:
    length = len(original)
    recorded = crc16_xmodem(original)
    intact_crc = CrcReading("valid", recorded) if length else None
    if length == 0 and kind in ("damaged", "truncated", "crc_unreadable", "crc_other"):
        kind = "intact"  # an Empty_Payload has nothing to damage or truncate

    if kind == "absent":
        return None
    if kind == "intact":
        return _obs(cid, block_no, original, sample_offset, data_crc=intact_crc, flags=flags)
    if kind == "damaged":
        damaged = _xor(original, draw(_flips(length)))
        return _obs(cid, block_no, damaged, sample_offset,
                    data_crc=_crc_reading(damaged, recorded), flags=flags)
    if kind == "truncated":
        cut = draw(st.integers(0, length - 1))
        return _obs(cid, block_no, original[:cut], sample_offset, expected_len=length,
                    data_crc=CrcReading("unreadable", None), flags=flags)
    if kind == "crc_unreadable":
        damaged = _xor(original, draw(_flips(length)))
        return _obs(cid, block_no, damaged, sample_offset,
                    data_crc=CrcReading("unreadable", None), flags=flags)
    if kind in ("header_invalid", "header_unreadable"):
        payload = draw(st.sampled_from([original, _xor(original, draw(_flips(length)))
                                        if length else original]))
        raw_no = draw(st.sampled_from([block_no, block_no + 1, block_no + 7]))
        return _obs(cid, block_no, payload, sample_offset,
                    data_crc=_crc_reading(payload, recorded) if length else None,
                    flags=flags, raw_block_no=raw_no,
                    header_outcome="invalid" if kind == "header_invalid" else "unreadable")
    if kind == "other_payload":
        other = _expand(b"other" + cid[:4].encode(), block_no, length)
        if other == original:
            other = bytes([original[0] ^ 1]) + original[1:] if length else other
        crc = CrcReading("valid", crc16_xmodem(other)) if length else None
        return _obs(cid, block_no, other, sample_offset, data_crc=crc, flags=flags)
    if kind == "header_disagree":
        if draw(st.booleans()):
            return _obs(cid, block_no, original, sample_offset, data_crc=intact_crc,
                        flags=flags ^ 0x01)
        return _obs(cid, block_no, original, sample_offset, data_crc=intact_crc,
                    flags=flags, load=LOAD ^ 0x100)
    if kind == "crc_other":
        damaged = _xor(original, draw(_flips(length)))
        other_crc = recorded ^ 0x0101
        return _obs(cid, block_no, damaged, sample_offset,
                    data_crc=_crc_reading(damaged, other_crc), flags=flags)
    raise AssertionError(kind)


@st.composite
def sessions(draw) -> list[CaptureObservations]:
    """One or two captures, sometimes with a duplicate copy, as generated."""
    n_caps = draw(st.integers(1, 2))
    cids = draw(st.lists(capture_ids, min_size=n_caps, max_size=n_caps, unique=True))
    n_blocks = draw(st.integers(2, 5))
    final_len = draw(st.integers(0, 40))
    seed = draw(st.binary(min_size=4, max_size=4))
    originals = [
        _expand(seed, n, 256 if n < n_blocks - 1 else final_len) for n in range(n_blocks)
    ]
    # Two captures usually share an intact block 0 (the anchor); sometimes not,
    # so PairingRejected is reached too.
    anchored = draw(st.integers(0, 5)) != 0

    reads: dict[str, list[BlockObservation]] = {cid: [] for cid in cids}
    for n, original in enumerate(originals):
        flags = FINAL_FLAG if n == n_blocks - 1 else 0
        # Sometimes every capture holds this block damaged with the original
        # CRC, so the bounded reconstruction path is reached often.
        all_damaged = n > 0 and draw(st.integers(0, 2)) == 0
        for cid in cids:
            base = 10_000 * (n + 1)
            if n == 0 and anchored:
                kind = "intact"
            elif all_damaged:
                kind = "damaged"
            else:
                kind = draw(st.sampled_from(READ_KINDS))
            first = draw(_read_of_kind(kind, cid, n, original, flags, base))
            if first is not None:
                reads[cid].append(first)
            # Optional re-read, either at a later offset or tied at the same one.
            if draw(st.integers(0, 4)) == 0:
                again_kind = draw(st.sampled_from(READ_KINDS))
                again_offset = base if draw(st.booleans()) else base + 500
                again = draw(_read_of_kind(again_kind, cid, n, original, flags, again_offset))
                if again is not None:
                    reads[cid].append(again)

    caps = [CaptureObservations(cid, tuple(reads[cid])) for cid in cids]
    # Optional duplicate of one capture: an exact copy, or (same capture ID)
    # a copy with one observation dropped.
    dup = draw(st.sampled_from(["none", "none", "exact", "subset"]))
    if dup != "none":
        src = draw(st.sampled_from(caps))
        obs = list(src.observations)
        if dup == "subset" and obs:
            del obs[draw(st.integers(0, len(obs) - 1))]
        caps.append(CaptureObservations(src.capture_id, tuple(obs)))
    return caps


@st.composite
def permuted(draw, caps: list[CaptureObservations]) -> list[CaptureObservations]:
    """The capture list and each capture's observations in a drawn order."""
    order = draw(st.permutations(caps))
    return [
        CaptureObservations(c.capture_id, tuple(draw(st.permutations(c.observations))))
        for c in order
    ]


@st.composite
def session_pairs(draw) -> tuple[list[CaptureObservations], list[CaptureObservations]]:
    caps = draw(sessions())
    return caps, draw(permuted(caps))


@st.composite
def unanchored_sessions(draw) -> list[CaptureObservations]:
    """Two captures that share no anchor: no block is intact in both with the
    same identity key and payload."""
    cid_a, cid_b = draw(st.lists(capture_ids, min_size=2, max_size=2, unique=True))
    n_blocks = draw(st.integers(1, 4))
    seed = draw(st.binary(min_size=4, max_size=4))
    reads: dict[str, list[BlockObservation]] = {cid_a: [], cid_b: []}
    for n in range(n_blocks):
        original = _expand(seed, n, draw(st.integers(1, 40)))
        flags = FINAL_FLAG if n == n_blocks - 1 else 0
        # One capture holds the block intact; the other holds it damaged,
        # with other bytes, unreadable, or not at all.
        intact_cid, other_cid = draw(st.permutations([cid_a, cid_b]))
        reads[intact_cid].append(
            draw(_read_of_kind("intact", intact_cid, n, original, flags, 10_000 * (n + 1)))
        )
        kind = draw(st.sampled_from(
            ["damaged", "other_payload", "crc_unreadable", "header_invalid", "absent"]
        ))
        other = draw(_read_of_kind(kind, other_cid, n, original, flags, 10_000 * (n + 1)))
        if other is not None:
            reads[other_cid].append(other)
    return [CaptureObservations(cid, tuple(reads[cid])) for cid in (cid_a, cid_b)]


# --- property -----------------------------------------------------------------


def _outcome(caps: list[CaptureObservations]):
    """Everything a caller can observe from one run, or the rejection."""
    try:
        result = recover(caps)
    except PairingRejected as exc:
        return ("rejected", exc.code, exc.message)
    decision = export_decision(result)
    text = dumps_report(build_report(result, decision))
    return ("ok", result, decision, text, result.program_payload())


@SETTINGS
@given(session_pairs())
def test_input_order_does_not_change_results(pair) -> None:
    caps, shuffled = pair
    first = _outcome(caps)
    second = _outcome(shuffled)
    event(f"outcome: {first[0]}")
    assert first[0] == second[0]
    if first[0] == "rejected":
        assert first == second
        return
    _, r1, d1, t1, p1 = first
    _, r2, d2, t2, p2 = second
    assert r1 == r2
    assert d1 == d2
    assert t1 == t2  # byte-identical report text
    assert p1 == p2
    # Reversing the generated order is a second, fixed permutation.
    assert _outcome(list(reversed(caps))) == first


@SETTINGS
@given(st.data())
def test_pairing_rejected_in_every_order(data) -> None:
    caps = data.draw(unanchored_sessions())
    shuffled = data.draw(permuted(caps))
    for attempt in (caps, shuffled, list(reversed(caps))):
        outcome = _outcome(attempt)
        assert outcome[:2] == ("rejected", "no_anchor"), outcome[0]


def test_generator_reaches_interesting_cases(fixed_constant_pool) -> None:
    """Fixed sample of the generator: the property must not hold only because
    every session is trivial. ``fixed_constant_pool`` (conftest.py) keeps the
    sample the same whichever other test modules pytest loaded."""
    seen: Counter[str] = Counter()

    @settings(max_examples=300, derandomize=True, deadline=None, database=None,
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
    @given(sessions())
    def collect(caps: list[CaptureObservations]) -> None:
        if len({c.capture_id for c in caps}) < len(caps):
            seen["duplicate"] += 1
        for c in caps:
            offsets = [o.sample_offset for o in c.observations]
            if len(offsets) != len(set(offsets)):
                seen["tied_offsets"] += 1
                break
        outcome = _outcome(caps)
        if outcome[0] == "rejected":
            seen["rejected"] += 1
            return
        result, decision = outcome[1], outcome[2]
        statuses = {b.status for b in result.blocks}
        seen.update(f"status:{s}" for s in statuses)
        if decision.permitted:
            seen["permitted"] += 1

    collect()
    for key, minimum in [
        ("rejected", 10),
        ("duplicate", 30),
        ("tied_offsets", 10),
        ("permitted", 5),
        (f"status:{RECONSTRUCTED}", 10),
        ("status:Intact", 50),
        ("status:Conflict", 10),
        ("status:Unresolved", 20),
        ("status:CRC failed", 10),
        ("status:Missing", 5),
    ]:
        assert seen[key] >= minimum, (key, dict(seen))
