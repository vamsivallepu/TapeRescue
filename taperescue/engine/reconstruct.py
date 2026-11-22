"""Bounded byte reconstruction for blocks that fail their data CRC in every read.

Given two or more equal-length payload reads that carry the same recorded data
CRC, the bytes on which every read agrees are kept. At each offset where the
reads differ, a Candidate takes one of the distinct observed values (sorted
ascending). The number of Candidates is the product of the distinct-value
counts and is computed before any Candidate is built. Above the cap nothing is
evaluated (``cap_exceeded``). Otherwise every Candidate from
``itertools.product`` is checked against the recorded CRC, without stopping at
the first match:

- zero matches give ``no_match``;
- one match gives the reconstructed payload, with the chosen value and the
  supporting capture IDs recorded for each differing offset;
- more than one match gives ``multiple_matches`` with the match count.

The recorded CRC is never replaced, and a payload is only ever built from
observed values. This module imports only the CRC routine and the shared type
aliases; it knows nothing about WAV files, the decoder, or the web layer.

Choosing which reads to pass in (valid headers, complete payloads, readable
and equal CRCs) is the caller's job (design steps h to k, task 12.2).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

from taperescue.crc import crc16_xmodem
from taperescue.models import UnresolvedReason

__all__ = [
    "OffsetChoice",
    "ReconstructionOutcome",
    "candidate_count",
    "differing_offsets",
    "reconstruct",
]


@dataclass(frozen=True)
class OffsetChoice:
    """One offset where the reads disagree.

    ``observed`` lists ``(capture_id, value)`` for every read, sorted by
    capture ID then value. ``chosen`` is set only when exactly one Candidate
    matched; ``supporting_capture_ids`` then holds every capture that observed
    the chosen value at this offset, sorted. Otherwise ``chosen`` is ``None``
    and ``supporting_capture_ids`` is empty.
    """

    offset: int
    observed: tuple[tuple[str, int], ...]
    chosen: int | None
    supporting_capture_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReconstructionOutcome:
    """Result of one bounded Candidate search.

    ``match_count`` is ``None`` when the search was skipped because the
    Candidate count exceeded the cap. ``payload`` is set only when exactly one
    Candidate matched, in which case ``reason`` is ``None``.
    """

    candidate_count: int
    evaluated_count: int
    match_count: int | None
    payload: bytes | None
    choices: tuple[OffsetChoice, ...]
    reason: UnresolvedReason | None

    @property
    def reconstructed(self) -> bool:
        return self.payload is not None and self.reason is None


def _check_lengths(payloads: Sequence[bytes]) -> int:
    if not payloads:
        raise ValueError("reconstruction needs at least one read")
    length = len(payloads[0])
    for p in payloads:
        if not isinstance(p, (bytes, bytearray, memoryview)):
            raise TypeError("payload reads must be bytes")
        if len(p) != length:
            raise ValueError("payload reads must all have the same length")
    return length


def differing_offsets(reads: Sequence[bytes]) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """Return ``(offset, distinct values sorted ascending)`` for every offset
    where the reads do not all agree, in offset order."""
    length = _check_lengths(reads)
    out = []
    for offset in range(length):
        values = {r[offset] for r in reads}
        if len(values) > 1:
            out.append((offset, tuple(sorted(values))))
    return tuple(out)


def candidate_count(reads: Sequence[bytes]) -> int:
    """Exact number of Candidates: the product of the distinct observed values
    at each differing offset (1 when every read agrees). No Candidate is built."""
    return math.prod(len(values) for _, values in differing_offsets(reads))


def _unchosen(offset: int, observed: tuple[tuple[str, int], ...]) -> OffsetChoice:
    return OffsetChoice(offset=offset, observed=observed, chosen=None, supporting_capture_ids=())


def reconstruct(
    reads: Sequence[tuple[str, bytes]],
    recorded_crc: int,
    cap: int,
) -> ReconstructionOutcome:
    """Search the Candidates formed from ``reads`` for the recorded data CRC.

    ``reads`` is a sequence of ``(capture_id, payload)`` pairs. Capture IDs
    are needed to attribute each chosen byte; the input order does not affect
    the outcome.
    """
    if isinstance(recorded_crc, bool) or not isinstance(recorded_crc, int):
        raise TypeError("recorded_crc must be an int")
    if not 0 <= recorded_crc <= 0xFFFF:
        raise ValueError(f"recorded_crc {recorded_crc} is outside 0..0xFFFF")
    if cap < 0:
        raise ValueError("cap must be non-negative")

    ordered = sorted(((cid, bytes(p)) for cid, p in reads), key=lambda r: (r[0], r[1]))
    payloads = [p for _, p in ordered]
    diffs = differing_offsets(payloads)
    count = math.prod(len(values) for _, values in diffs)

    observed_at = {
        offset: tuple(sorted((cid, p[offset]) for cid, p in ordered)) for offset, _ in diffs
    }

    if count > cap:
        return ReconstructionOutcome(
            candidate_count=count,
            evaluated_count=0,
            match_count=None,
            payload=None,
            choices=tuple(_unchosen(o, observed_at[o]) for o, _ in diffs),
            reason="cap_exceeded",
        )

    template = bytearray(payloads[0])
    offsets = [o for o, _ in diffs]
    matches: list[tuple[int, ...]] = []
    evaluated = 0
    for combo in itertools.product(*(values for _, values in diffs)):
        for offset, value in zip(offsets, combo):
            template[offset] = value
        evaluated += 1
        if crc16_xmodem(template) == recorded_crc:
            matches.append(combo)

    if len(matches) == 1:
        combo = matches[0]
        payload = bytearray(payloads[0])
        choices = []
        for offset, value in zip(offsets, combo):
            payload[offset] = value
            observed = observed_at[offset]
            supporting = tuple(sorted({cid for cid, v in observed if v == value}))
            choices.append(
                OffsetChoice(
                    offset=offset,
                    observed=observed,
                    chosen=value,
                    supporting_capture_ids=supporting,
                )
            )
        return ReconstructionOutcome(
            candidate_count=count,
            evaluated_count=evaluated,
            match_count=1,
            payload=bytes(payload),
            choices=tuple(choices),
            reason=None,
        )

    return ReconstructionOutcome(
        candidate_count=count,
        evaluated_count=evaluated,
        match_count=len(matches),
        payload=None,
        choices=tuple(_unchosen(o, observed_at[o]) for o, _ in diffs),
        reason="no_match" if not matches else "multiple_matches",
    )
