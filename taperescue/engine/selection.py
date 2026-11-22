"""Per-block decision steps for the recovery engine.

Each step takes a :class:`BlockContext` and returns a :class:`BlockResult` when
its condition holds, or ``None`` to pass the block to the next step. The steps
are applied in the order listed in the design's per-block decision table
(``taperescue.engine.DECISION_STEPS``).

This module implements steps a (``Missing``), b (``header_invalid``),
e (``Intact``) and f (single capture, ``CRC failed`` / ``single_read``).
Steps c and d (header and payload conflicts) live in ``matching.py``.
Steps g to l (intact-only mode, evidence checks, bounded reconstruction) live
in ``taperescue/engine/__init__.py`` and call ``reconstruct.py``.

Only data models are imported here. Nothing in the engine touches WAV files,
the decoder, or the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass

from taperescue.models import BlockObservation, BlockResult, RecoverOptions


@dataclass(frozen=True)
class BlockContext:
    """Everything a decision step may look at for one required block number.

    ``reads`` holds every observation placed at this block number, valid header
    or not, sorted by ``(capture_id, sample_offset)``. ``valid_reads`` is the
    subset whose header CRC is valid; only these may supply identity, bytes,
    or CRC values.
    """

    block_no: int
    reads: tuple[BlockObservation, ...]
    capture_count: int
    options: RecoverOptions

    @property
    def valid_reads(self) -> tuple[BlockObservation, ...]:
        return tuple(r for r in self.reads if r.header_crc.outcome == "valid")

    @property
    def intact_reads(self) -> tuple[BlockObservation, ...]:
        return tuple(r for r in self.reads if r.is_intact)


def _result(ctx: BlockContext, status, reason, *, payload=None, supporting=()) -> BlockResult:
    return BlockResult(
        block_no=ctx.block_no,
        status=status,
        reason=reason,
        payload=payload,
        supporting_capture_ids=tuple(supporting),
        reads=ctx.reads,
        conflict=None,
        reconstruction=None,
    )


def step_a_missing(ctx: BlockContext) -> BlockResult | None:
    """a: no read of any kind was placed at this block number."""
    if ctx.reads:
        return None
    return _result(ctx, "Missing", "block_missing")


def step_b_header_invalid(ctx: BlockContext) -> BlockResult | None:
    """b: reads exist, but none has a valid header CRC."""
    if ctx.valid_reads:
        return None
    return _result(ctx, "Unresolved", "header_invalid")


def step_e_intact(ctx: BlockContext) -> BlockResult | None:
    """e: one or more Intact_Blocks with equal payloads.

    Supporting capture IDs are every capture holding an intact read of that
    payload, sorted. Empty payloads are retained as ``b""`` (Requirement 8.4).
    When intact payloads differ this step declines; step d runs first and
    decides that case as a payload conflict.
    """
    intact = ctx.intact_reads
    if not intact:
        return None
    payloads = {r.payload for r in intact}
    if len(payloads) != 1:
        return None
    supporting = sorted({r.capture_id for r in intact})
    return _result(ctx, "Intact", None, payload=intact[0].payload, supporting=supporting)


def step_f_single_capture(ctx: BlockContext) -> BlockResult | None:
    """f: the session holds one capture and no read of this block is intact.

    One recording cannot supply the second read that reconstruction needs.
    """
    if ctx.capture_count != 1 or ctx.intact_reads:
        return None
    return _result(ctx, "CRC failed", "single_read")


def undecided(ctx: BlockContext) -> BlockResult:
    """Safety net for a block that no decision step decided.

    Step l always decides, so this is not reached in normal operation. It
    never reports a payload, so such a block can never be exported.
    """
    return _result(ctx, "CRC failed", None)
