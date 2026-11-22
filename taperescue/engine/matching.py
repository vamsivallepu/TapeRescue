"""Capture matching: identity keys, anchors, and conflict decision steps.

Only reads with a valid header CRC are identity candidates (Requirement 7.1).
Their identity key is ``(filename, load_addr, exec_addr, block_no, block_len,
flags)``; ``next_addr`` is reported but not matched (Requirement 7.2).

With two or more captures, anchors are computed before any other block is
paired (Requirement 7.3). A session without an anchor is rejected with
:class:`PairingRejected` (Requirement 7.4). A single capture skips the anchor
check.

Decision steps c and d live here and are wired into
``taperescue.engine.DECISION_STEPS`` between steps b and e:

* c: valid-header reads of one block number disagree on the identity key
  -> ``Conflict``, ``header_disagreement``, excluded from reconstruction
  (Requirement 7.5);
* d: intact reads with agreeing headers carry different payloads
  -> ``Conflict``, ``conflict.kind = "payload"`` with every capture ID that
  holds an intact read (Requirement 7.6).

Only data models are imported here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from taperescue.engine.selection import BlockContext
from taperescue.models import BlockObservation, BlockResult, CaptureObservations

__all__ = [
    "NO_ANCHOR_MESSAGE",
    "PairingRejected",
    "compute_anchors",
    "identity_key",
    "require_anchor",
    "step_c_header_disagreement",
    "step_d_payload_conflict",
]

NO_ANCHOR_MESSAGE = (
    "These recordings cannot be shown to belong to the same program. "
    "Select recordings of the same program version."
)


class PairingRejected(Exception):
    """Two or more captures share no anchor block."""

    def __init__(self, code: Literal["no_anchor"] = "no_anchor",
                 message: str = NO_ANCHOR_MESSAGE) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def identity_key(obs: BlockObservation) -> tuple | None:
    """Identity key of a read, or ``None`` when its header CRC is not valid."""
    if obs.header_crc.outcome != "valid" or obs.header is None:
        return None
    return obs.header.identity_key()


# --- anchors -----------------------------------------------------------------


def _intact_evidence(cap: CaptureObservations) -> set[tuple[int, tuple, bytes]]:
    """``(block_no, identity_key, payload)`` for every intact read in one capture."""
    out: set[tuple[int, tuple, bytes]] = set()
    for obs in cap.observations:
        key = identity_key(obs)
        if key is not None and obs.is_intact:
            out.add((obs.header.block_no, key, obs.payload))  # type: ignore[union-attr]
    return out


def compute_anchors(captures: Sequence[CaptureObservations]) -> tuple[int, ...]:
    """Sorted block numbers at which every capture has an Intact_Block with the
    same identity key and the same payload bytes.

    Returns ``()`` for fewer than two captures: one recording cannot anchor
    itself. Captures are expected to be deduplicated already.
    """
    if len(captures) < 2:
        return ()
    shared = _intact_evidence(captures[0])
    for cap in captures[1:]:
        shared &= _intact_evidence(cap)
        if not shared:
            break
    return tuple(sorted({block_no for block_no, _, _ in shared}))


def require_anchor(captures: Sequence[CaptureObservations]) -> tuple[int, ...]:
    """Anchors for a session; raises :class:`PairingRejected` when two or more
    captures share none."""
    anchors = compute_anchors(captures)
    if len(captures) >= 2 and not anchors:
        raise PairingRejected()
    return anchors


# --- decision steps c and d --------------------------------------------------


def _conflict(ctx: BlockContext, reason, kind: str, capture_ids) -> BlockResult:
    return BlockResult(
        block_no=ctx.block_no,
        status="Conflict",
        reason=reason,
        payload=None,
        supporting_capture_ids=(),
        reads=ctx.reads,
        conflict={"kind": kind, "capture_ids": sorted(set(capture_ids))},
        reconstruction=None,
    )


def step_c_header_disagreement(ctx: BlockContext) -> BlockResult | None:
    """c: valid-header reads of this block number carry different identity keys.

    ``conflict.capture_ids`` lists every capture holding a valid-header read of
    the block. No reconstruction is attempted (``reconstruction`` is ``None``).
    """
    valid = ctx.valid_reads
    if len({identity_key(r) for r in valid}) <= 1:
        return None
    return _conflict(ctx, "header_disagreement", "header", (r.capture_id for r in valid))


def step_d_payload_conflict(ctx: BlockContext) -> BlockResult | None:
    """d: intact reads with agreeing headers carry different payload bytes.

    ``conflict.capture_ids`` lists every capture holding an intact read of the
    block. The reason is ``None``: ``Conflict`` is the status, and the
    Unresolved_Reason codes have no payload-conflict entry.
    """
    intact = ctx.intact_reads
    if len({r.payload for r in intact}) <= 1:
        return None
    return _conflict(ctx, None, "payload", (r.capture_id for r in intact))
