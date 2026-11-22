"""Post-write verification of an exported UEF image (Requirement 11.4).

``verify_export(path, result)`` reads the image with the independent reader
(:mod:`taperescue.uef.reader`, which has its own bitwise CRC) and checks it
against the recovery result:

* the file holds exactly one block for each of ``0..final``, in order;
* each block's header bytes equal the ``header_raw`` of the block's
  lowest-``(capture_id, sample_offset)`` valid-header read, and its header CRC
  equals that read's recorded header CRC;
* each non-empty block's data CRC equals a data CRC recorded by one of the
  block's valid-header reads, and an empty block has no data CRC;
* both CRCs recompute correctly from the file bytes;
* each block's payload equals the selected payload, and the concatenated
  payload equals ``result.program_payload()``.

On any failure the file at ``path`` is deleted and
:class:`ExportVerificationFailed` is raised with the list of problems.

This module imports only the data models and the reader. It does not import
the exporter or ``taperescue.crc``, so it derives the expected values itself.
"""

from __future__ import annotations

from pathlib import Path

from taperescue.models import BlockResult, RecoveryResult
from taperescue.uef.reader import UefBlock, UefFormatError, read_uef

__all__ = ["ExportVerificationFailed", "verify_export"]


class ExportVerificationFailed(Exception):
    """The exported image does not match the recovery result."""

    def __init__(self, problems: list[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("Exported image failed verification: " + "; ".join(self.problems))


def _block_problems(n: int, block: BlockResult, fb: UefBlock) -> list[str]:
    problems: list[str] = []
    valid = sorted(
        (r for r in block.reads if r.header_crc.outcome == "valid" and r.header is not None),
        key=lambda r: (r.capture_id, r.sample_offset),
    )
    if not valid:
        return [f"block {n}: no valid-header read in the result"]
    source = valid[0]
    if fb.header_raw != source.header_raw:
        problems.append(f"block {n}: header bytes differ from the captured header")
    if fb.header_crc_in_file != source.header_crc.recorded:
        problems.append(
            f"block {n}: header CRC in file 0x{fb.header_crc_in_file:04X} is not the "
            f"recorded 0x{source.header_crc.recorded:04X}"
        )
    if not fb.header_crc_ok:
        problems.append(f"block {n}: header CRC does not match the header bytes")
    if fb.payload != block.payload:
        problems.append(f"block {n}: payload differs from the recovered payload")
    if not block.payload:
        if fb.data_crc_in_file is not None:
            problems.append(f"block {n}: empty block carries a data CRC")
    else:
        recorded = {
            r.data_crc.recorded
            for r in valid
            if r.data_crc is not None and r.data_crc.outcome != "unreadable"
        }
        if fb.data_crc_in_file not in recorded:
            problems.append(
                f"block {n}: data CRC in file is not a CRC recorded in the captures"
            )
        if not fb.data_crc_ok:
            problems.append(f"block {n}: data CRC does not match the payload bytes")
    return problems


def _problems(path: Path, result: RecoveryResult) -> list[str]:
    program = result.program_payload()
    if program is None or result.final_block is None:
        return ["the recovery result has no complete program payload"]
    try:
        blocks = read_uef(path)
    except (OSError, UefFormatError) as exc:
        return [f"cannot read the image: {exc}"]
    expected = result.final_block + 1
    if len(blocks) != expected:
        return [f"image holds {len(blocks)} blocks, expected {expected}"]
    by_no = {b.block_no: b for b in result.blocks}
    problems: list[str] = []
    for n, fb in enumerate(blocks):
        problems.extend(_block_problems(n, by_no[n], fb))
    if b"".join(fb.payload for fb in blocks) != program:
        problems.append("concatenated payload differs from the recovered program")
    return problems


def verify_export(path: Path, result: RecoveryResult) -> None:
    """Check the image at ``path``; delete it and raise on any mismatch."""
    path = Path(path)
    problems = _problems(path, result)
    if problems:
        path.unlink(missing_ok=True)
        raise ExportVerificationFailed(problems)
