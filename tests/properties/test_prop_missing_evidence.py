"""Property 17: missing evidence never produces a repair.

# Feature: tape-rescue, Property 17: Missing evidence never produces a repair

For any block in which a valid-header read is truncated, a data CRC is
unreadable, or the reads carry different recorded CRCs, the status is
``Unresolved`` with ``truncated_payload``, ``crc_unreadable`` or
``crc_mismatch_between_reads``, the block payload is absent, and no chosen
bytes or substitute CRC exist for that block. The generator also covers the
neighbouring evidence defects the user asked for: identity fields that
disagree between captures, a block held by only one capture, and intact-only
mode.

Each generated session has two captures with every block except the target
intact in both, so the anchor check passes and the target block is the only
thing under test. When several defects are combined, the expected reason is
the first one in the design's per-block decision table (steps c, g, h, i, j,
k), which is the order of :data:`DEFECTS`.

The damaged reads are built so that, without the defect, the bounded search
would find the original payload (disjoint damaged offsets, original CRC
recorded). Truncated reads are undamaged prefixes of the original, and the
original's tail is often zero, so an engine that zero-padded truncated reads
would rebuild the original and report a CRC match.

**Validates: Requirements 9.1, 9.8, 9.9, 9.10, 9.12, 12.5, 13.3**
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st

from taperescue.crc import crc16_xmodem
from taperescue.engine import recover
from taperescue.engine.gate import export_decision
from taperescue.fixtures.block_builder import encode_header
from taperescue.models import (
    BlockObservation,
    CaptureObservations,
    CrcReading,
    HeaderFields,
    RecoverOptions,
)

FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023
FULL = 256
FINAL_FLAG = 0x80
HEX = "0123456789abcdef"

# Precedence order: design steps c, g, h, i, j, k.
DEFECTS = (
    "header_disagreement",
    "intact_only",
    "one_capture",
    "truncated",
    "crc_unreadable",
    "crc_mismatch",
)

# defect -> (status, engine reason, gate blocker reason)
EXPECTED = {
    "header_disagreement": ("Conflict", "header_disagreement", "header_disagreement"),
    "intact_only": ("CRC failed", None, "crc_failed"),
    "one_capture": ("Unresolved", "single_read", "single_read"),
    "truncated": ("Unresolved", "truncated_payload", "truncated_payload"),
    "crc_unreadable": ("Unresolved", "crc_unreadable", "crc_unreadable"),
    "crc_mismatch": ("Unresolved", "crc_mismatch_between_reads", "crc_mismatch_between_reads"),
}


# --- observation builders -----------------------------------------------------


def make_obs(
    cid: str,
    block_no: int,
    payload: bytes,
    *,
    expected_len: int,
    flags: int,
    data_crc: CrcReading | None,
    header_ok: bool = True,
    filename: str = FILENAME,
    load: int = LOAD,
    exec_: int = EXEC,
) -> BlockObservation:
    raw = encode_header(filename, load, exec_, block_no, expected_len, flags, 0)
    hcrc = crc16_xmodem(raw)
    header = HeaderFields(filename, load, exec_, block_no, expected_len, flags, 0)
    return BlockObservation(
        capture_id=cid,
        sample_offset=100_000 * (block_no + 1),
        header_raw=raw,
        header=header if header_ok else None,
        header_crc=CrcReading("valid", hcrc) if header_ok else CrcReading("invalid", hcrc ^ 0x0101),
        payload=payload,
        expected_len=expected_len,
        actual_len=len(payload),
        data_crc=data_crc,
    )


def intact_obs(cid: str, block_no: int, payload: bytes, flags: int) -> BlockObservation:
    return make_obs(
        cid, block_no, payload, expected_len=len(payload), flags=flags,
        data_crc=CrcReading("valid", crc16_xmodem(payload)),
    )


def xor_at(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


# --- scenario strategy ----------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    captures: tuple[CaptureObservations, ...]
    options: RecoverOptions
    target: int
    defects: frozenset[str]
    expected: str                       # key into EXPECTED
    target_reads: tuple[BlockObservation, ...]
    original: bytes
    zero_tail: int


@st.composite
def scenarios(draw) -> Scenario:
    n_blocks = draw(st.integers(2, 5))
    target = draw(st.integers(0, n_blocks - 1))
    final_no = n_blocks - 1
    lengths = [FULL] * final_no + [draw(st.integers(1, FULL))]
    rng = random.Random(draw(st.integers(0, 2**32 - 1)))
    payloads = [rng.randbytes(n) for n in lengths]

    # Target payload, often ending in zeros so zero padding would look right.
    length = lengths[target]
    zero_tail = draw(st.one_of(st.just(0), st.integers(1, length)))
    original = draw(st.binary(min_size=length - zero_tail, max_size=length - zero_tail))
    original += bytes(zero_tail)
    payloads[target] = original
    crc_orig = crc16_xmodem(original)

    cid_a, cid_b = draw(st.lists(
        st.text(HEX, min_size=64, max_size=64), min_size=2, max_size=2, unique=True,
    ))

    # The deciding defect is drawn uniformly so every step gets coverage; extra
    # defects come only from later steps, which the deciding one must outrank.
    expected = draw(st.sampled_from(DEFECTS))
    later = [d for d in DEFECTS[DEFECTS.index(expected) + 1:]
             if not (expected == "header_disagreement" and d == "one_capture")]
    defects = {expected} | draw(st.sets(st.sampled_from(later))) if later else {expected}
    if "one_capture" in defects:
        # crc_mismatch needs a second valid-header read.
        defects.discard("crc_mismatch")
    assert expected == next(d for d in DEFECTS if d in defects)

    # Damage at disjoint offsets, so the defect-free search would find `original`.
    offsets = draw(st.lists(st.integers(0, length - 1), min_size=1,
                            max_size=min(length, 6), unique=True))
    split = draw(st.integers(1, len(offsets)))
    a_offsets = offsets[:split]
    b_offsets = offsets[split:] or offsets[:1]
    masks = st.integers(1, 255)
    reads = {
        "A": xor_at(original, {o: draw(masks) for o in a_offsets}),
        "B": xor_at(original, {o: draw(masks) for o in b_offsets}),
    }
    crcs = {"A": CrcReading("invalid", crc_orig), "B": CrcReading("invalid", crc_orig)}
    valid_names = ["A"] if "one_capture" in defects else ["A", "B"]

    if "truncated" in defects:
        for name in draw(st.sets(st.sampled_from(valid_names), min_size=1)):
            in_zero_tail = zero_tail > 0 and draw(st.booleans())
            lo = length - zero_tail if in_zero_tail else 0
            cut = draw(st.integers(lo, length - 1))
            reads[name] = original[:cut]   # undamaged prefix, suffix never observed
    if "crc_unreadable" in defects:
        for name in draw(st.sets(st.sampled_from(valid_names), min_size=1)):
            crcs[name] = CrcReading("unreadable", None)
    if "crc_mismatch" in defects:
        readable = [n for n in ("B", "A") if crcs[n].outcome != "unreadable"]
        if readable:
            delta = draw(st.integers(1, 0xFFFF))
            crcs[readable[0]] = CrcReading("invalid", crc_orig ^ delta)

    flags_t = FINAL_FLAG if target == final_no else 0
    b_header: dict = {}
    if "header_disagreement" in defects:
        field = draw(st.sampled_from(["filename", "load", "exec", "flags"]))
        if field == "filename":
            b_header["filename"] = draw(st.sampled_from(["STARMAQ", "STARMAP2", "S", "OTHER"]))
        elif field == "load":
            b_header["load"] = LOAD ^ draw(st.integers(1, 0xFFFFFFFF))
        elif field == "exec":
            b_header["exec_"] = EXEC ^ draw(st.integers(1, 0xFFFFFFFF))
        else:
            # Low flag bits only: bit 7 decides the final block for the whole session.
            b_header["flags"] = flags_t ^ (1 << draw(st.integers(0, 5)))

    target_reads: list[BlockObservation] = []
    caps = []
    for name, cid in (("A", cid_a), ("B", cid_b)):
        observations = []
        for k in range(n_blocks):
            flags = FINAL_FLAG if k == final_no else 0
            if k != target:
                observations.append(intact_obs(cid, k, payloads[k], flags))
                continue
            if name in valid_names:
                extra = b_header if name == "B" else {}
                read = make_obs(cid, k, reads[name], expected_len=length,
                                data_crc=crcs[name], **{"flags": flags, **extra})
            elif draw(st.booleans()):
                # Invalid header CRC but a perfect payload: it must supply nothing.
                read = make_obs(cid, k, original, expected_len=length, flags=flags,
                                data_crc=CrcReading("valid", crc_orig), header_ok=False)
            else:
                continue  # block absent from this capture
            observations.append(read)
            target_reads.append(read)
        caps.append(CaptureObservations(capture_id=cid, observations=tuple(observations)))
    if draw(st.booleans()):
        caps.reverse()

    return Scenario(
        captures=tuple(caps),
        options=RecoverOptions(reconstruct="intact_only" not in defects),
        target=target,
        defects=frozenset(defects),
        expected=expected,
        target_reads=tuple(target_reads),
        original=original,
        zero_tail=zero_tail,
    )


# --- the property ---------------------------------------------------------------


def _key(r: BlockObservation) -> tuple:
    return (r.capture_id, r.sample_offset, repr(r))


@settings(
    max_examples=500,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(scenarios())
def test_missing_evidence_never_produces_a_repair(sc: Scenario) -> None:
    """Feature: tape-rescue, Property 17: Missing evidence never produces a repair."""
    note(f"defects={sorted(sc.defects)} expected={sc.expected} target={sc.target}")
    event(f"expected={sc.expected}")
    status, reason, gate_reason = EXPECTED[sc.expected]

    result = recover(sc.captures, sc.options)
    blocks = {b.block_no: b for b in result.blocks}
    block = blocks[sc.target]

    # Never a repair, never a claimed candidate.
    assert block.status != "Reconstructed · CRC match"
    assert (block.status, block.reason) == (status, reason)
    assert block.reconstruction is None          # no search, so no choices or match count
    assert block.payload is None
    assert not block.is_recovered
    assert block.supporting_capture_ids == ()
    if sc.expected == "header_disagreement":
        assert block.conflict is not None and block.conflict["kind"] == "header"
    else:
        assert block.conflict is None

    # The reads are reported exactly as observed: no padding, no substitute CRC.
    assert sorted(block.reads, key=_key) == sorted(sc.target_reads, key=_key)
    for r in block.reads:
        assert len(r.payload) == r.actual_len <= r.expected_len
    if "truncated" in sc.defects:
        assert any(r.actual_len < r.expected_len for r in block.reads)

    # Every other block is intact, so the target is the only thing stopping export.
    assert all(b.status == "Intact" for n, b in blocks.items() if n != sc.target)
    assert result.program_payload() is None

    decision = export_decision(result)
    assert not decision.permitted
    assert decision.blockers == ((sc.target, gate_reason),)
    assert sc.target not in decision.reconstructed_blocks


# --- example: the zero-padding trap is armed --------------------------------------


def test_truncated_zero_suffix_is_not_padded() -> None:
    """A truncated read whose missing tail is zero in the original stays unresolved.

    The control session gives the same read already zero-padded (as a padding
    decoder or engine would) and shows that this would yield a CRC match, so
    the property above would catch padding.
    """
    rng = random.Random(17)
    original = rng.randbytes(200) + bytes(56)
    crc = crc16_xmodem(original)
    tail = rng.randbytes(40)
    a, b = "a" * 64, "b" * 64

    def session(a_payload: bytes) -> tuple[CaptureObservations, ...]:
        caps = []
        for cid, payload in ((a, a_payload), (b, xor_at(original, {10: 0x04}))):
            caps.append(CaptureObservations(cid, (
                make_obs(cid, 0, payload, expected_len=FULL, flags=0,
                         data_crc=CrcReading("invalid", crc)),
                intact_obs(cid, 1, tail, FINAL_FLAG),
            )))
        return tuple(caps)

    truncated = recover(session(original[:200])).blocks[0]
    assert (truncated.status, truncated.reason, truncated.payload) == (
        "Unresolved", "truncated_payload", None)
    assert export_decision(recover(session(original[:200]))).blockers == (
        (0, "truncated_payload"),)

    padded = recover(session(original[:200] + bytes(56))).blocks[0]
    assert padded.status == "Reconstructed · CRC match"
    assert padded.payload == original
