"""Property 21: export gate.

# Feature: tape-rescue, Property 21: Export gate

For any recovery result, ``export_decision`` permits export if and only if a
final-flag block is known and every block from 0 through it is ``Intact`` or
``Reconstructed · CRC match`` with consistent headers and sequence. Otherwise
``blockers`` lists the failing block numbers with reasons, and ``export_uef``
raises ``ExportRefused`` without writing a file.

Two generators feed the property:

* ``direct_cases`` builds ``RecoveryResult`` objects by hand. Each required
  block gets a random status and, when it is recovered, at most one planted
  defect: a header field that differs from the file's, a wrong header block
  number, a stray or missing final flag, a bad declared length, a payload
  whose length differs from the header, no payload, no valid-header read,
  disagreeing headers, or a duplicate block result. The file may also have no
  final flag, or a final flag on an earlier block with later blocks of the same
  file still on tape. The generator records which ``(block, reason)`` blockers
  each choice must produce, so the expected blocker set is known exactly
  unless the planted header identities disagree (then the gate's choice of
  which block to blame is a tie-break and only refusal is checked).
* ``engine_cases`` builds one or two captures of observations (intact,
  damaged, truncated, unreadable CRC, invalid header, absent), with optional
  renamed headers and missing or early final flags, and runs ``recover``. The
  expected decision is derived from the result's statuses and headers.

**Validates: Requirements 11.1, 11.2, 13.4**
"""

from __future__ import annotations

import os
import random
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st

from taperescue.crc import crc16_xmodem
from taperescue.engine import recover
from taperescue.engine.gate import ExportDecision, export_decision
from taperescue.models import (
    RECOVERED_STATUSES,
    BlockObservation,
    BlockResult,
    CaptureObservations,
    CrcReading,
    HeaderFields,
    RecoverOptions,
    RecoveryResult,
)
from taperescue.uef.exporter import ExportRefused, export_uef
from taperescue.uef.reader import read_uef

FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023
STANDARD_ID = (FILENAME, LOAD, EXEC)
FULL = 256
FINAL_FLAG = 0x80
EMPTY_FLAG = 0x40
HEX = "0123456789abcdef"
RECONSTRUCTED = "Reconstructed · CRC match"

# Reason the gate reports when the engine left ``reason`` unset.
FALLBACK = {
    "CRC failed": "crc_failed",
    "Conflict": "payload_conflict",
    "Unresolved": "unresolved",
    "Missing": "block_missing",
}

SETTINGS = settings(
    max_examples=300,
    derandomize=True,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,  # tmp_path is only a parent; each example uses a fresh subdir
    ],
)

capture_ids = st.lists(st.text(HEX, min_size=64, max_size=64), min_size=2, max_size=2, unique=True)


# --- observation builders ---------------------------------------------------------


def raw_header(filename: str, load: int, exec_: int, block_no: int, block_len: int,
               flags: int) -> bytes:
    """Header bytes from the filename through the next-file address.

    Written locally (not with the fixture encoder) so a declared length over
    256 can be planted.
    """
    return filename.encode("ascii") + b"\x00" + struct.pack(
        "<IIHHBI", load, exec_, block_no, block_len, flags, 0
    )


def make_read(
    cid: str,
    offset: int,
    payload: bytes,
    *,
    block_no: int,
    block_len: int | None = None,
    flags: int = 0,
    identity: tuple[str, int, int] = STANDARD_ID,
    header_ok: bool = True,
    data_crc: CrcReading | None | str = "auto",
) -> BlockObservation:
    """One observation. ``data_crc="auto"`` gives a valid CRC of ``payload``
    (``None`` for an empty block)."""
    filename, load, exec_ = identity
    length = len(payload) if block_len is None else block_len
    raw = raw_header(filename, load, exec_, block_no, length, flags)
    hcrc = crc16_xmodem(raw)
    if data_crc == "auto":
        data_crc = CrcReading("valid", crc16_xmodem(payload)) if length else None
    return BlockObservation(
        capture_id=cid,
        sample_offset=offset,
        header_raw=raw,
        header=HeaderFields(filename, load, exec_, block_no, length, flags, 0) if header_ok else None,
        header_crc=CrcReading("valid", hcrc) if header_ok else CrcReading("invalid", hcrc ^ 0x0101),
        payload=payload,
        expected_len=length,
        actual_len=len(payload),
        data_crc=data_crc,  # type: ignore[arg-type]
    )


def flip(payload: bytes, offset: int, mask: int) -> bytes:
    out = bytearray(payload)
    out[offset % len(out)] ^= mask
    return bytes(out)


def block_result(n: int, status: str, reason, payload, reads, supporting=()) -> BlockResult:
    return BlockResult(
        block_no=n,
        status=status,  # type: ignore[arg-type]
        reason=reason,
        payload=payload,
        supporting_capture_ids=tuple(sorted(supporting)),
        reads=tuple(sorted(reads, key=lambda r: (r.capture_id, r.sample_offset))),
        conflict=None,
        reconstruction=None,
    )


# --- generator 1: hand-built recovery results ------------------------------------------

STATUS_CHOICES = (
    ["Intact"] * 4 + [RECONSTRUCTED] * 2 + ["CRC failed", "Unresolved", "Conflict", "Missing"]
)
NON_RECOVERED_REASONS = {
    "CRC failed": [None, "single_read"],
    "Unresolved": [None, "no_match", "multiple_matches", "cap_exceeded", "crc_unreadable",
                   "crc_mismatch_between_reads", "truncated_payload", "header_invalid",
                   "single_read"],
    "Conflict": [None, "header_disagreement"],
    "Missing": [None, "block_missing"],
}
# Defects that stop the gate before it looks at header fields, with their reason.
UNUSABLE_DEFECTS = {
    "payload_none": "payload_missing",
    "header_invalid_only": "header_invalid",
    "header_disagreement": "header_disagreement",
    "duplicate": "duplicate_block",
}
COMMON_DEFECTS = ["block_no_mismatch", "payload_len", *UNUSABLE_DEFECTS]
IDENTITY_DEFECTS = {
    "filename": ("OTHER", LOAD, EXEC),
    "load": (FILENAME, LOAD ^ 0x100, EXEC),
    "exec": (FILENAME, LOAD, EXEC ^ 0x100),
}


@dataclass(frozen=True)
class DirectCase:
    result: RecoveryResult
    expected_permitted: bool
    expected_blockers: frozenset[tuple[int, str]]
    exact: bool            # False when planted identities disagree
    synthetic: int | None  # block number of the "final block never seen" entry
    summary: str


@st.composite
def direct_cases(draw) -> DirectCase:
    cid_a, cid_b = draw(capture_ids)
    k = draw(st.integers(1, 6))
    mode = draw(st.sampled_from(["normal"] * 3 + ["none", "early"])) if k > 1 else \
        draw(st.sampled_from(["normal"] * 3 + ["none"]))
    final = {"normal": k - 1, "none": None, "early": None}[mode]
    if mode == "early":
        final = draw(st.integers(0, k - 2))
    rng = random.Random(draw(st.integers(0, 2**32 - 1)))
    last_len = draw(st.integers(0, FULL))
    lengths = [FULL] * (k - 1) + [last_len]
    payloads = [rng.randbytes(n) for n in lengths]
    required = range(final + 1) if final is not None else range(k)

    blocks: list[BlockResult] = []
    reads_a: list[BlockObservation] = []
    reads_b: list[BlockObservation] = []
    expected: set[tuple[int, str]] = set()
    usable_ids: set[tuple] = set()
    labels: list[str] = []

    for n in required:
        length = lengths[n]
        payload = payloads[n]
        flags = (FINAL_FLAG if n == final else 0) | (EMPTY_FLAG if length == 0 else 0)
        off = 10_000 * (n + 1)
        status = draw(st.sampled_from(STATUS_CHOICES))
        if status == RECONSTRUCTED and length == 0:
            status = "Intact"

        if status not in RECOVERED_STATUSES:
            reason = draw(st.sampled_from(NON_RECOVERED_REASONS[status]))
            reads = []
            if status != "Missing":
                crc = CrcReading("invalid", crc16_xmodem(payload)) if length else None
                damaged = flip(payload, n, 0x10) if length else payload
                reads.append(make_read(cid_a, off, damaged, block_no=n, flags=flags, data_crc=crc))
                if status != "CRC failed":
                    reads.append(make_read(cid_b, off, payload, block_no=n, flags=flags))
            # A payload on a non-recovered block must not unlock export.
            kept = payload if draw(st.booleans()) else None
            blocks.append(block_result(n, status, reason, kept, reads))
            expected.add((n, reason or FALLBACK[status]))
            labels.append(f"{n}:{status}")
            for r in reads:
                (reads_a if r.capture_id == cid_a else reads_b).append(r)
            continue

        applicable = list(COMMON_DEFECTS) + list(IDENTITY_DEFECTS)
        applicable += ["final_flag_missing", "oversize_final"] if n == final else \
            ["stray_final_flag", "short_block"]
        defect = draw(st.one_of(st.just(None), st.just(None), st.sampled_from(applicable)))
        labels.append(f"{n}:{status}:{defect}")

        identity = IDENTITY_DEFECTS.get(defect, STANDARD_ID)
        hdr_no = n + draw(st.integers(1, 50)) if defect == "block_no_mismatch" else n
        if defect == "stray_final_flag":
            flags |= FINAL_FLAG
        if defect == "final_flag_missing":
            flags &= ~FINAL_FLAG
        if defect == "short_block":
            payload = payload[:FULL - 1]
        if defect == "oversize_final":
            payload = rng.randbytes(FULL + 1)
        header_ok = defect != "header_invalid_only"
        hdr = dict(block_no=hdr_no, flags=flags, identity=identity, header_ok=header_ok)

        if status == "Intact":
            reads = [make_read(cid_a, off, payload, **hdr)]
            supporting = [cid_a]
        else:
            crc = CrcReading("invalid", crc16_xmodem(payload))
            reads = [
                make_read(cid_a, off, flip(payload, rng.randrange(len(payload)), 0x04),
                          data_crc=crc, **hdr),
                make_read(cid_b, off, flip(payload, rng.randrange(len(payload)), 0x40),
                          data_crc=crc, **hdr),
            ]
            supporting = [cid_a, cid_b]
        if defect == "header_disagreement":
            other = (identity[0], identity[1], identity[2] ^ 1)
            reads.append(make_read(cid_b, off + 1, payload, block_no=hdr_no, flags=flags,
                                   identity=other))
        block_payload = payload
        if defect == "payload_len":
            block_payload = payload + b"\x00"
        if defect == "payload_none":
            block_payload = None
        result_block = block_result(n, status, None, block_payload, reads, supporting)
        blocks.append(result_block)
        if defect == "duplicate":
            blocks.append(result_block)
        for r in reads:
            (reads_a if r.capture_id == cid_a else reads_b).append(r)

        if defect in UNUSABLE_DEFECTS:
            expected.add((n, UNUSABLE_DEFECTS[defect]))
            continue
        # A usable header: these are the sequence and length rules of Requirement 11.1.
        usable_ids.add(identity)
        declared = len(payload)
        if hdr_no != n:
            expected.add((n, "block_no_mismatch"))
        if n == final:
            if not flags & FINAL_FLAG:
                expected.add((n, "final_flag_missing"))
            if declared > FULL:
                expected.add((n, "block_len_invalid"))
        else:
            if flags & FINAL_FLAG:
                expected.add((n, "unexpected_final_flag"))
            if declared != FULL:
                expected.add((n, "block_len_invalid"))
        if len(block_payload) != declared:  # type: ignore[arg-type]
            expected.add((n, "payload_length_mismatch"))

    synthetic = None
    if mode == "none":
        synthetic = len(required)
        expected.add((synthetic, "block_missing"))
    if mode == "early":
        # The rest of the same file is still on tape, past the flagged block.
        for n in range(final + 1, k):
            flags = (FINAL_FLAG if n == k - 1 else 0) | (EMPTY_FLAG if lengths[n] == 0 else 0)
            reads_a.append(make_read(cid_a, 10_000 * (n + 1), payloads[n], block_no=n, flags=flags))
        if usable_ids == {STANDARD_ID}:
            expected.add((final, "unexpected_final_flag"))

    exact = len(usable_ids) <= 1
    result = RecoveryResult(
        captures=(
            CaptureObservations(cid_a, tuple(reads_a)),
            CaptureObservations(cid_b, tuple(reads_b)),
        ),
        duplicates_merged=(),
        anchors=(),
        final_block=final,
        blocks=tuple(blocks),
        options=RecoverOptions(),
    )
    return DirectCase(
        result=result,
        expected_permitted=mode == "normal" and not expected and exact,
        expected_blockers=frozenset(expected),
        exact=exact,
        synthetic=synthetic,
        summary=f"mode={mode} k={k} final={final} blocks={labels}",
    )


# --- generator 2: observations through recover() -------------------------------------

KINDS = ["intact"] * 4 + ["damaged", "damaged", "truncated", "crc_unreadable",
                          "header_invalid", "absent"]


@st.composite
def engine_cases(draw) -> tuple[list[CaptureObservations], str]:
    n_caps = draw(st.integers(1, 2))
    cids = draw(capture_ids)[:n_caps]
    k = draw(st.integers(1, 6))
    mode = draw(st.sampled_from(["normal"] * 3 + ["none"] + (["early"] if k > 1 else [])))
    early_at = draw(st.integers(0, k - 2)) if mode == "early" else None
    early_caps = set(draw(st.sets(st.sampled_from(cids), min_size=1))) if mode == "early" else set()
    anchor = draw(st.integers(0, k - 1)) if n_caps == 2 else None
    if anchor is not None and anchor == early_at:
        early_caps = set(cids)  # the anchor's identity key must match in both captures
    rename = None
    if draw(st.booleans()):
        rename = (draw(st.sampled_from(cids)), draw(st.integers(0, k - 1)))
        if rename[1] == anchor:
            rename = None

    rng = random.Random(draw(st.integers(0, 2**32 - 1)))
    lengths = [FULL] * (k - 1) + [draw(st.integers(0, FULL))]
    payloads = [rng.randbytes(n) for n in lengths]
    kinds_seen: list[str] = []

    # Repairable pairs: both captures damaged at different offsets, so the
    # bounded search usually yields a Reconstructed block.
    pair_damage: dict[int, list[int]] = {}
    if n_caps == 2:
        for n in range(k):
            if n != anchor and lengths[n] >= 2 and draw(st.integers(0, 3)) == 0:
                pair_damage[n] = draw(st.lists(st.integers(0, lengths[n] - 1),
                                               min_size=2, max_size=2, unique=True))

    caps = []
    for ci, cid in enumerate(cids):
        obs = []
        for n in range(k):
            length, payload = lengths[n], payloads[n]
            kind = "intact" if n == anchor else draw(st.sampled_from(KINDS))
            if n in pair_damage:
                kind = "pair_damaged"
            if length == 0 and kind in ("damaged", "truncated", "crc_unreadable"):
                kind = "intact"
            kinds_seen.append(kind)
            if kind == "absent":
                continue
            flags = EMPTY_FLAG if length == 0 else 0
            if mode != "none" and n == k - 1:
                flags |= FINAL_FLAG
            if n == early_at and cid in early_caps:
                flags |= FINAL_FLAG
            identity = ("OTHER", LOAD, EXEC) if rename == (cid, n) else STANDARD_ID
            common = dict(block_no=n, block_len=length, flags=flags, identity=identity)
            off = 10_000 * (n + 1)
            crc = crc16_xmodem(payload)
            if kind == "intact":
                read = make_read(cid, off, payload, **common)
            elif kind == "damaged":
                damaged = payload
                for _ in range(draw(st.integers(1, 2))):
                    damaged = flip(damaged, draw(st.integers(0, length - 1)), draw(st.integers(1, 255)))
                read = make_read(cid, off, damaged, data_crc=CrcReading("invalid", crc), **common)
            elif kind == "pair_damaged":
                damaged = flip(payload, pair_damage[n][ci], draw(st.integers(1, 255)))
                read = make_read(cid, off, damaged, data_crc=CrcReading("invalid", crc), **common)
            elif kind == "truncated":
                cut = draw(st.integers(0, length - 1))
                read = make_read(cid, off, payload[:cut], data_crc=CrcReading("invalid", crc), **common)
            elif kind == "crc_unreadable":
                read = make_read(cid, off, payload, data_crc=CrcReading("unreadable", None), **common)
            else:  # header_invalid: a perfect payload that must supply nothing
                read = make_read(cid, off, payload, header_ok=False, **common)
            obs.append(read)
        caps.append(CaptureObservations(cid, tuple(obs)))
    if draw(st.booleans()):
        caps.reverse()
    summary = (f"caps={n_caps} k={k} mode={mode} early_at={early_at} anchor={anchor} "
               f"rename={rename} kinds={kinds_seen}")
    return caps, summary


# --- shared checks --------------------------------------------------------------------


def _chosen_header(block: BlockResult) -> HeaderFields | None:
    valid = sorted(
        (r for r in block.reads if r.header_crc.outcome == "valid" and r.header is not None),
        key=lambda r: (r.capture_id, r.sample_offset),
    )
    return valid[0].header if valid else None


def _check_common(result: RecoveryResult, decision: ExportDecision) -> None:
    """Rules that hold for every result, however it was built."""
    blockers = decision.blockers
    # Sorted, no repeats, deterministic, and independent of list order.
    assert list(blockers) == sorted(set(blockers))
    assert export_decision(result) == decision
    shuffled = RecoveryResult(
        captures=tuple(reversed(result.captures)),
        duplicates_merged=result.duplicates_merged,
        anchors=result.anchors,
        final_block=result.final_block,
        blocks=tuple(reversed(result.blocks)),
        options=result.options,
    )
    assert export_decision(shuffled) == decision

    assert decision.permitted == (not blockers)
    numbers = {b.block_no for b in result.blocks}
    required = range(result.final_block + 1) if result.final_block is not None \
        else range(max(numbers, default=-1) + 1)
    blocked = {n for n, _ in blockers}

    # Requirement 13.4: every non-recovered required block blocks export, with its reason.
    for block in result.blocks:
        if block.block_no in required and block.status not in RECOVERED_STATUSES:
            assert (block.block_no, block.reason or FALLBACK[block.status]) in blockers
    for n in required:
        if n not in numbers:
            assert (n, "block_missing") in blockers
    # Requirement 11.2: no flagged final block means refusal.
    if result.final_block is None:
        assert not decision.permitted
        assert (len(required), "block_missing") in blockers
        assert blocked <= set(required) | {len(required)}
    else:
        assert blocked <= set(required)

    counts = {n: sum(b.block_no == n for b in result.blocks) for n in required}
    assert decision.reconstructed_blocks == tuple(
        b.block_no for b in sorted(result.blocks, key=lambda b: b.block_no)
        if b.block_no in required and counts[b.block_no] == 1 and b.status == RECONSTRUCTED
    )
    if decision.permitted:
        assert result.program_payload() is not None


def _check_export(result: RecoveryResult, decision: ExportDecision, parent: Path) -> None:
    with tempfile.TemporaryDirectory(dir=parent) as d:
        out = Path(d) / "tape.uef"
        if not decision.permitted:
            with pytest.raises(ExportRefused) as exc:
                export_uef(result, decision, out)
            assert exc.value.blockers == decision.blockers
            # A stale "permitted" decision cannot unlock export either.
            forged = ExportDecision(permitted=True, blockers=(), reconstructed_blocks=())
            with pytest.raises(ExportRefused):
                export_uef(result, forged, out)
            assert os.listdir(d) == []
        else:
            export_uef(result, decision, out)  # verifies with the independent reader
            assert os.listdir(d) == ["tape.uef"]
            assert b"".join(b.payload for b in read_uef(out)) == result.program_payload()


# --- the properties -------------------------------------------------------------------


@SETTINGS
@given(case=direct_cases())
def test_export_gate_on_built_results(case: DirectCase, tmp_path: Path) -> None:
    """Feature: tape-rescue, Property 21: Export gate (hand-built recovery results)."""
    note(case.summary)
    decision = export_decision(case.result)
    note(f"blockers={decision.blockers}")
    event(f"permitted={decision.permitted}")
    event(f"exact={case.exact}")
    for _, reason in decision.blockers:
        event(f"blocker={reason}")

    assert decision.permitted == case.expected_permitted
    if case.exact:
        assert set(decision.blockers) == case.expected_blockers
    else:
        # Headers disagree on filename or address: refused, and every planted
        # status, sequence or length blocker is still listed.
        assert not decision.permitted
        assert case.expected_blockers <= set(decision.blockers)
    _check_common(case.result, decision)
    _check_export(case.result, decision, tmp_path)


@SETTINGS
@given(case=engine_cases())
def test_export_gate_on_recovered_captures(case, tmp_path: Path) -> None:
    """Feature: tape-rescue, Property 21: Export gate (results from ``recover``)."""
    captures, summary = case
    note(summary)
    result = recover(captures)
    decision = export_decision(result)
    note(f"statuses={[(b.block_no, b.status, b.reason) for b in result.blocks]}")
    note(f"final={result.final_block} blockers={decision.blockers}")
    event(f"permitted={decision.permitted}")
    for b in result.blocks:
        event(f"status={b.status}")
    for _, reason in decision.blockers:
        event(f"blocker={reason}")

    final = result.final_block
    all_recovered = final is not None and all(b.status in RECOVERED_STATUSES for b in result.blocks)
    ids = set()
    for b in result.blocks:
        h = _chosen_header(b)
        if b.status in RECOVERED_STATUSES and h is not None:
            ids.add((h.filename, h.load_addr, h.exec_addr))
    beyond = final is not None and len(ids) == 1 and any(
        o.header_crc.outcome == "valid" and o.header is not None and o.header.block_no > final
        and (o.header.filename, o.header.load_addr, o.header.exec_addr) in ids
        for cap in result.captures for o in cap.observations
    )
    expected = all_recovered and len(ids) == 1 and not beyond
    assert decision.permitted == expected
    if all_recovered and beyond:
        # The flagged block is not the end of the file on tape.
        assert (final, "unexpected_final_flag") in decision.blockers
    _check_common(result, decision)
    _check_export(result, decision, tmp_path)
