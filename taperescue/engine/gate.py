"""Export gate: decides whether a recovery result may be written as a ``.uef``.

``export_decision(result)`` is a pure function over a :class:`RecoveryResult`.
It imports only the data models. ``RecoveryResult`` does not carry the
decision; callers (report, exporter, CLI, API) call this function themselves.

Export is permitted only when all of these hold (Requirements 11.1, 11.2):

* a final block is known, i.e. some valid-header read carries flag bit 7;
* every block ``0..final`` is ``Intact`` or ``Reconstructed · CRC match``;
* every required block's valid-header reads agree on one header, and the
  required blocks share one filename, load address and execution address;
* each block's header ``block_no`` equals its position;
* only the final block has flag bit 7, and no unplaced valid-header read of
  the same file exists past the final block;
* every block before the final one declares 256 bytes, the final block
  declares at most 256, and each payload length equals the declared length.

Otherwise ``blockers`` lists ``(block_no, reason)`` pairs sorted by block
number, then reason. A reason is the block's ``UnresolvedReason`` when the
engine set one, or one of the gate codes in :data:`GATE_REASONS`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from taperescue.models import BlockObservation, BlockResult, HeaderFields, RecoveryResult

__all__ = ["ExportDecision", "GATE_REASONS", "export_decision"]

FINAL_FLAG = 0x80
FULL_BLOCK_LEN = 256

# Reasons used when the engine left ``reason`` unset for a non-recovered status.
_STATUS_FALLBACK = {
    "CRC failed": "crc_failed",
    "Conflict": "payload_conflict",
    "Unresolved": "unresolved",
    "Missing": "block_missing",
}

GATE_REASONS: tuple[str, ...] = (
    "block_missing",          # no result for a required block, or no final block known
    "duplicate_block",        # more than one result for one block number
    "crc_failed",             # CRC failed with no engine reason (intact-only mode)
    "payload_conflict",       # Conflict with no engine reason
    "unresolved",             # Unresolved with no engine reason
    "payload_missing",        # recovered status but no payload
    "header_invalid",         # recovered status but no valid-header read
    "header_disagreement",    # valid-header reads of one block disagree
    "filename_mismatch",
    "address_mismatch",       # load or execution address differs from the file's
    "block_no_mismatch",      # header block number differs from the position
    "unexpected_final_flag",  # flag bit 7 before the end of the file
    "final_flag_missing",     # the final block's header lacks flag bit 7
    "block_len_invalid",      # non-final block not 256 bytes, or final block over 256
    "payload_length_mismatch",
)


@dataclass(frozen=True)
class ExportDecision:
    permitted: bool
    blockers: tuple[tuple[int, str], ...]   # (block_no, reason code), sorted
    reconstructed_blocks: tuple[int, ...]


def export_decision(result: RecoveryResult) -> ExportDecision:
    final = result.final_block
    blockers: set[tuple[int, str]] = set()

    by_no: dict[int, list[BlockResult]] = {}
    for block in result.blocks:
        by_no.setdefault(block.block_no, []).append(block)

    if final is not None:
        required = range(final + 1)
    else:
        last = max(by_no, default=-1)
        required = range(last + 1)
        # Synthetic entry for the final block that was never seen.
        blockers.add((last + 1, "block_missing"))

    headers: dict[int, HeaderFields] = {}
    for n in required:
        entries = by_no.get(n, [])
        if not entries:
            blockers.add((n, "block_missing"))
            continue
        if len(entries) > 1:
            blockers.add((n, "duplicate_block"))
            continue
        block = entries[0]
        problem = _status_problem(block)
        if problem is not None:
            blockers.add((n, problem))
            continue
        header, problem = _block_header(block)
        if problem is not None:
            blockers.add((n, problem))
            continue
        headers[n] = header  # type: ignore[assignment]

    file_id = _file_identity(headers)
    for n, header in headers.items():
        blockers.update((n, reason) for reason in _header_problems(n, header, final, file_id))
        payload_len = len(by_no[n][0].payload)  # type: ignore[arg-type]
        if payload_len != header.block_len:
            blockers.add((n, "payload_length_mismatch"))

    if final is not None and file_id is not None:
        placed = {r for n in required for b in by_no.get(n, []) for r in b.reads}
        if _reads_after(result, final, file_id, placed):
            blockers.add((final, "unexpected_final_flag"))

    reconstructed = tuple(
        n for n in required
        if len(by_no.get(n, [])) == 1 and by_no[n][0].status == "Reconstructed · CRC match"
    )
    ordered = tuple(sorted(blockers))
    return ExportDecision(
        permitted=not ordered,
        blockers=ordered,
        reconstructed_blocks=reconstructed,
    )


def _status_problem(block: BlockResult) -> str | None:
    if block.status in ("Intact", "Reconstructed · CRC match"):
        return None if block.payload is not None else "payload_missing"
    return block.reason or _STATUS_FALLBACK.get(block.status, "unresolved")


def _block_header(block: BlockResult) -> tuple[HeaderFields | None, str | None]:
    """Header the exporter would use: the lowest-capture-ID valid-header read.

    Every valid-header read of the block must carry the same identity key.
    """
    valid = sorted(
        (r for r in block.reads if r.header_crc.outcome == "valid" and r.header is not None),
        key=lambda r: (r.capture_id, r.sample_offset),
    )
    if not valid:
        return None, "header_invalid"
    if len({r.header.identity_key() for r in valid}) > 1:  # type: ignore[union-attr]
        return None, "header_disagreement"
    return valid[0].header, None


def _file_identity(headers: dict[int, HeaderFields]) -> tuple[str, int, int] | None:
    """Most common ``(filename, load, exec)`` among usable headers.

    Ties go to the identity seen at the lowest block number, so the odd block
    out is the one blamed and the choice is deterministic.
    """
    if not headers:
        return None
    ids = {n: (h.filename, h.load_addr, h.exec_addr) for n, h in headers.items()}
    counts = Counter(ids.values())
    first_seen: dict[tuple, int] = {}
    for n in sorted(ids):
        first_seen.setdefault(ids[n], n)
    return min(counts, key=lambda i: (-counts[i], first_seen[i]))


def _header_problems(
    n: int, header: HeaderFields, final: int | None, file_id: tuple | None
) -> Iterable[str]:
    if file_id is not None:
        if header.filename != file_id[0]:
            yield "filename_mismatch"
        if (header.load_addr, header.exec_addr) != file_id[1:]:
            yield "address_mismatch"
    if header.block_no != n:
        yield "block_no_mismatch"
    flagged = bool(header.flags & FINAL_FLAG)
    if n == final:
        if not flagged:
            yield "final_flag_missing"
        if header.block_len > FULL_BLOCK_LEN:
            yield "block_len_invalid"
    else:
        if flagged:
            yield "unexpected_final_flag"
        if header.block_len != FULL_BLOCK_LEN:
            yield "block_len_invalid"


def _reads_after(
    result: RecoveryResult, final: int, file_id: tuple, placed: set[BlockObservation]
) -> bool:
    """True if a valid-header read of the same file lies past the final block.

    That means the flagged block is not the end of the file, so the flag is on
    a non-final block. Reads already placed in a required block are skipped;
    a wrong header number on those is reported as ``block_no_mismatch``.
    """
    for cap in result.captures:
        for obs in cap.observations:
            h = obs.header
            if (
                obs not in placed
                and obs.header_crc.outcome == "valid"
                and h is not None
                and h.block_no > final
                and (h.filename, h.load_addr, h.exec_addr) == file_id
            ):
                return True
    return False
