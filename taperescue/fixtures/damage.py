"""Damage_Injector: flip single payload bits in a block's byte frames.

Damage is applied to the frame list from
:func:`taperescue.fixtures.block_builder.block_frames` before modulation. Only
the ``value`` of targeted payload frames changes. The sync byte, header bytes,
header CRC, and the recorded data CRC frames are left exactly as built, so a
damaged recording still carries the original data CRC (Requirement 5.3). The
frame count, order, roles, and offsets are unchanged, and the modulator frames
every byte with the same start bit, stop bit, and bit timing, so damage cannot
alter framing or timing (Requirement 5.4). CRCs are never recomputed.

Rules enforced by :func:`apply_damage`:

* every flip names the block being damaged;
* every flip targets an existing payload frame (by payload offset);
* no payload offset is flipped twice, so each damaged byte differs from the
  original in exactly one bit;
* each flip names one bit index, 0 to 7.

Checking that two fixtures use disjoint offsets (Requirement 5.2) is the
fixture generator's job, since it needs both damage plans.

This module must not import ``taperescue.decoder_adapter`` (Requirement 4.8).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from taperescue.fixtures.block_builder import ByteFrame

__all__ = ["BitFlip", "apply_damage"]


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    return value


@dataclass(frozen=True)
class BitFlip:
    """Flip bit ``bit`` (0 is least significant) of payload byte ``offset`` in block ``block_no``."""

    block_no: int
    offset: int      # payload offset
    bit: int         # 0..7

    def __post_init__(self) -> None:
        block_no = _require_int("block_no", self.block_no)
        offset = _require_int("offset", self.offset)
        bit = _require_int("bit", self.bit)
        if not 0 <= block_no <= 0xFFFF:
            raise ValueError(f"block_no must fit in 16 bits, got {block_no}")
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if not 0 <= bit <= 7:
            raise ValueError(f"bit must be a single bit index 0 to 7, got {bit}")

    @property
    def mask(self) -> int:
        return 1 << self.bit


def apply_damage(frames: Sequence[ByteFrame], block_no: int,
                 flips: Sequence[BitFlip]) -> list[ByteFrame]:
    """Return a copy of ``frames`` (the frames of block ``block_no``) with ``flips`` applied.

    Raises ``ValueError`` if a flip names another block, targets a payload
    offset that the block does not have, or repeats an offset. The input list
    is not modified.
    """
    _require_int("block_no", block_no)
    payload_index: dict[int, int] = {}
    for i, frame in enumerate(frames):
        if frame.role == "payload":
            if frame.offset in payload_index:
                raise ValueError(f"block {block_no} has two payload frames at offset {frame.offset}")
            payload_index[frame.offset] = i

    damaged = list(frames)
    seen: set[int] = set()
    for flip in flips:
        if not isinstance(flip, BitFlip):
            raise ValueError(f"expected a BitFlip, got {flip!r}")
        if flip.block_no != block_no:
            raise ValueError(
                f"flip targets block {flip.block_no}, but these frames are block {block_no}"
            )
        if flip.offset in seen:
            raise ValueError(
                f"payload offset {flip.offset} of block {block_no} is flipped more than once"
            )
        seen.add(flip.offset)
        index = payload_index.get(flip.offset)
        if index is None:
            raise ValueError(
                f"payload offset {flip.offset} does not exist in block {block_no} "
                f"({len(payload_index)} payload bytes); only payload bytes may be damaged"
            )
        original = frames[index]
        damaged[index] = ByteFrame(original.value ^ flip.mask, "payload", original.offset)
    return damaged
