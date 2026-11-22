"""Recovery engine: matching, intact selection, bounded reconstruction, export gate, report.

``recover(captures, options)`` is a pure function over immutable
``CaptureObservations``. It imports only the data models and sibling engine
modules, never the decoder adapter, WAV handling, fixtures, UEF code, or the
web server.

Pipeline:

1. Normalise inputs: drop repeated capture IDs into ``duplicates_merged``,
   sort captures by ID and each capture's reads by ``(block_no, sample_offset)``.
2. With two or more captures, compute anchors (see ``matching.py``) and raise
   ``PairingRejected("no_anchor")`` when there are none.
3. Place every read at a block number (see :func:`_placement`).
4. Compute the required block range from the lowest flagged final block.
5. Apply :data:`DECISION_STEPS` to each required block; the first step that
   returns a result decides the block.

Steps g to l (defined below) handle blocks that no capture holds intact.
Only valid-header reads supply bytes or CRC values. A block that is not
reconstructed carries ``payload = None`` and the engine never computes a
substitute CRC: the reconstruction search compares candidates against the
CRC recorded on tape, and that value is only reported, never replaced.

``reconstruction is None`` means no candidate search was attempted (steps a
to k). Step g is the intact-only baseline and records exactly that.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from taperescue.engine.matching import (
    PairingRejected,
    require_anchor,
    step_c_header_disagreement,
    step_d_payload_conflict,
)
# Imported as a module: binding the function name ``reconstruct`` here would
# shadow the ``taperescue.engine.reconstruct`` submodule attribute.
from taperescue.engine import reconstruct as _reconstruct
from taperescue.engine.selection import (
    BlockContext,
    step_a_missing,
    step_b_header_invalid,
    step_e_intact,
    step_f_single_capture,
    undecided,
)
from taperescue.models import (
    BlockObservation,
    BlockResult,
    CaptureObservations,
    RecoverOptions,
    RecoveryResult,
)

__all__ = ["DECISION_STEPS", "PairingRejected", "RecoverOptions", "recover"]

FINAL_FLAG = 0x80

DecisionStep = Callable[[BlockContext], "BlockResult | None"]


# --- decision steps g to l ----------------------------------------------------
#
# These run only after steps a to f declined, so every block reaching them has
# at least one valid-header read, agreeing identity keys (step c), no intact
# read (steps d and e), and two or more captures in the session (step f).


def _unrecovered(ctx: BlockContext, status, reason, reconstruction=None) -> BlockResult:
    """A block result with no payload and no supporting captures."""
    return BlockResult(
        block_no=ctx.block_no,
        status=status,
        reason=reason,
        payload=None,
        supporting_capture_ids=(),
        reads=ctx.reads,
        conflict=None,
        reconstruction=reconstruction,
    )


def step_g_intact_only(ctx: BlockContext) -> BlockResult | None:
    """g: intact-only baseline (``options.reconstruct`` is false).

    ``CRC failed`` with no reason and ``reconstruction = None``, which the
    report renders as ``attempted: false`` (Requirement 14.2).
    """
    if ctx.options.reconstruct:
        return None
    return _unrecovered(ctx, "CRC failed", None)


def step_h_single_read(ctx: BlockContext) -> BlockResult | None:
    """h: fewer than two valid-header reads, so there is nothing to combine."""
    if len(ctx.valid_reads) >= 2:
        return None
    return _unrecovered(ctx, "Unresolved", "single_read")


def step_i_truncated(ctx: BlockContext) -> BlockResult | None:
    """i: a valid-header read holds fewer bytes than its header declares.

    A read whose length differs from the header's ``block_len`` is treated the
    same way: the missing positions were never observed (Requirement 9.10, 9.12).
    """
    for r in ctx.valid_reads:
        if r.payload_state == "truncated" or len(r.payload) != r.header.block_len:  # type: ignore[union-attr]
            return _unrecovered(ctx, "Unresolved", "truncated_payload")
    return None


def step_j_crc_unreadable(ctx: BlockContext) -> BlockResult | None:
    """j: a valid-header read has no readable recorded data CRC (Requirement 9.8)."""
    for r in ctx.valid_reads:
        if r.data_crc is None or r.data_crc.outcome == "unreadable":
            return _unrecovered(ctx, "Unresolved", "crc_unreadable")
    return None


def step_k_crc_mismatch(ctx: BlockContext) -> BlockResult | None:
    """k: the valid-header reads recorded different data CRC values (Requirement 9.9)."""
    if len({r.data_crc.recorded for r in ctx.valid_reads}) <= 1:  # type: ignore[union-attr]
        return None
    return _unrecovered(ctx, "Unresolved", "crc_mismatch_between_reads")


def step_l_reconstruct(ctx: BlockContext) -> BlockResult:
    """l: bounded candidate search over the valid-header reads.

    Every read here has an agreeing header, a complete payload of the declared
    length, and the same readable recorded data CRC. Exactly one matching
    candidate gives ``Reconstructed · CRC match`` with the payload, per-offset
    choices, and the sorted IDs of the captures whose reads entered the
    search. ``no_match``, ``multiple_matches`` and ``cap_exceeded`` give
    ``Unresolved`` with the outcome attached and no payload.
    """
    valid = ctx.valid_reads
    recorded = valid[0].data_crc.recorded  # type: ignore[union-attr]
    outcome = _reconstruct.reconstruct(
        [(r.capture_id, r.payload) for r in valid],
        recorded,  # type: ignore[arg-type]
        ctx.options.candidate_cap,
    )
    if not outcome.reconstructed:
        return _unrecovered(ctx, "Unresolved", outcome.reason, outcome)
    return BlockResult(
        block_no=ctx.block_no,
        status="Reconstructed · CRC match",
        reason=None,
        payload=outcome.payload,
        supporting_capture_ids=tuple(sorted({r.capture_id for r in valid})),
        reads=ctx.reads,
        conflict=None,
        reconstruction=outcome,
    )


# Ordered per the design's per-block decision table. Steps c and d run before
# g to l, so a Conflict block never reaches reconstruction. Step l always
# decides, so ``undecided`` is only a safety net.
DECISION_STEPS: tuple[DecisionStep, ...] = (
    step_a_missing,
    step_b_header_invalid,
    step_c_header_disagreement,
    step_d_payload_conflict,
    step_e_intact,
    step_f_single_capture,
    step_g_intact_only,
    step_h_single_read,
    step_i_truncated,
    step_j_crc_unreadable,
    step_k_crc_mismatch,
    step_l_reconstruct,
)


def recover(
    captures: Sequence[CaptureObservations],
    options: RecoverOptions = RecoverOptions(),
) -> RecoveryResult:
    """Merge one or more captures into per-block results.

    Raises ``ValueError`` when no capture is supplied and ``PairingRejected``
    when two or more distinct captures share no anchor block.
    """
    if not captures:
        raise ValueError("recover needs at least one capture")
    for cap in captures:
        if not isinstance(cap, CaptureObservations):
            raise TypeError(f"expected CaptureObservations, got {type(cap).__name__}")

    normalised, duplicates = _normalise(captures)
    # Anchors are established before any other block is paired. Raises
    # PairingRejected when two or more captures share none.
    anchors = require_anchor(normalised)
    placed = _place_all(normalised)
    final_block = _final_block(placed)
    required = _required_blocks(placed, final_block)

    by_block: dict[int, list[BlockObservation]] = {}
    for obs, block_no in placed:
        if block_no is not None:
            by_block.setdefault(block_no, []).append(obs)

    blocks: list[BlockResult] = []
    for n in required:
        reads = tuple(sorted(by_block.get(n, ()), key=lambda r: (r.capture_id, r.sample_offset)))
        ctx = BlockContext(block_no=n, reads=reads, capture_count=len(normalised), options=options)
        blocks.append(_decide(ctx))

    return RecoveryResult(
        captures=tuple(normalised),
        duplicates_merged=duplicates,
        anchors=anchors,
        final_block=final_block,
        blocks=tuple(blocks),
        options=options,
    )


def _decide(ctx: BlockContext) -> BlockResult:
    for step in DECISION_STEPS:
        result = step(ctx)
        if result is not None:
            return result
    return undecided(ctx)


# --- normalisation -----------------------------------------------------------


def _normalise(
    captures: Sequence[CaptureObservations],
) -> tuple[list[CaptureObservations], tuple[str, ...]]:
    """Deduplicate by capture ID, sort reads, sort captures by ID.

    Captures with the same ID come from byte-identical WAV files. If their
    observation lists still differ, the copy with the smallest canonical form is
    kept so the choice never depends on input order.
    """
    groups: dict[str, list[CaptureObservations]] = {}
    for cap in captures:
        groups.setdefault(cap.capture_id, []).append(_sort_reads(cap))

    kept: list[CaptureObservations] = []
    duplicates: list[str] = []
    for cid in sorted(groups):
        copies = groups[cid]
        kept.append(min(copies, key=lambda c: repr(c.observations)))
        duplicates.extend([cid] * (len(copies) - 1))
    return kept, tuple(duplicates)


def _sort_reads(cap: CaptureObservations) -> CaptureObservations:
    placements = _placement(cap.observations)
    order = sorted(
        range(len(cap.observations)),
        key=lambda i: _read_sort_key(cap.observations[i], placements[i]),
    )
    return CaptureObservations(
        capture_id=cap.capture_id,
        observations=tuple(cap.observations[i] for i in order),
    )


def _read_sort_key(obs: BlockObservation, block_no: int | None) -> tuple:
    # Unplaced reads sort after every placed read; repr breaks exact ties.
    placed = 0 if block_no is not None else 1
    return (placed, block_no if block_no is not None else 0, obs.sample_offset, repr(obs))


# --- placement of reads at block numbers ------------------------------------


def _place_all(captures: Sequence[CaptureObservations]) -> list[tuple[BlockObservation, int | None]]:
    out: list[tuple[BlockObservation, int | None]] = []
    for cap in captures:
        out.extend(zip(cap.observations, _placement(cap.observations)))
    return out


def _placement(reads: Sequence[BlockObservation]) -> list[int | None]:
    """Block number for each read, aligned with ``reads``.

    A read with a valid header CRC is placed at its parsed block number.
    A read with an invalid or unreadable header CRC supplies no identity, but it
    is still listed under the block it most likely belongs to so the report can
    show it. Its position is inferred within the same capture from the nearest
    valid-header reads before and after it (by sample offset):

    * the block number in its raw header bytes, if it lies between the
      neighbouring valid block numbers (inclusive, so a failed re-read of a
      block next to a good read of the same block is placed with it), or is at
      most one past the last valid block when nothing valid follows. The bound
      stops a corrupted block number from inflating the block range;
    * otherwise the single block number between the neighbours, if they are
      exactly two apart (or the next valid block is 1 and nothing valid
      precedes it);
    * otherwise ``None`` (unplaced; kept in the capture but in no block).
    """
    by_time = sorted(range(len(reads)), key=lambda i: (reads[i].sample_offset, repr(reads[i])))
    result: list[int | None] = [None] * len(reads)
    valid_positions = [i for i in by_time if reads[i].header_crc.outcome == "valid"]
    for i in valid_positions:
        result[i] = reads[i].header.block_no  # type: ignore[union-attr]

    for pos, i in enumerate(by_time):
        if reads[i].header_crc.outcome == "valid":
            continue
        prev_no = next(
            (result[j] for j in reversed(by_time[:pos]) if reads[j].header_crc.outcome == "valid"),
            -1,
        )
        next_no = next(
            (result[j] for j in by_time[pos + 1:] if reads[j].header_crc.outcome == "valid"),
            None,
        )
        raw_no = _raw_block_no(reads[i].header_raw)
        upper = next_no if next_no is not None else prev_no + 1
        if raw_no is not None and max(prev_no, 0) <= raw_no <= upper:
            result[i] = raw_no
        elif next_no is not None and next_no - prev_no == 2:
            result[i] = prev_no + 1
    return result


def _raw_block_no(header_raw: bytes) -> int | None:
    """Unverified block number from raw header bytes, used only for placement.

    Layout: filename (1-10 bytes), NUL, load (4), exec (4), block number (2 LE).
    """
    nul = header_raw.find(b"\x00", 0, 11)
    if nul < 1:
        return None
    start = nul + 1 + 8
    if len(header_raw) < start + 2:
        return None
    return int.from_bytes(header_raw[start:start + 2], "little")


# --- block range --------------------------------------------------------------


def _final_block(placed: Sequence[tuple[BlockObservation, int | None]]) -> int | None:
    """Lowest block number among valid-header reads with flag bit 7 set."""
    flagged = [
        obs.header.block_no
        for obs, _ in placed
        if obs.header_crc.outcome == "valid"
        and obs.header is not None
        and obs.header.flags & FINAL_FLAG
    ]
    return min(flagged) if flagged else None


def _required_blocks(
    placed: Sequence[tuple[BlockObservation, int | None]], final_block: int | None
) -> range:
    """``0..final`` when a final block is known, else ``0..max(block_no)``.

    Without a flagged block the maximum is taken over every placed read; the
    export gate then refuses export (task 9.2).
    """
    if final_block is not None:
        return range(final_block + 1)
    numbers = [n for _, n in placed if n is not None]
    return range(max(numbers) + 1) if numbers else range(0)
