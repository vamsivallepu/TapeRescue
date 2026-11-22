"""UEF tape image exporter.

``export_uef(result, decision, out)`` writes a recovered program as an
uncompressed UEF 0.10 image, then checks it with
:func:`taperescue.uef.verify.verify_export`. It refuses with
:class:`ExportRefused` unless the export gate permits the result.

Every CRC byte written is a value recorded in the input captures
(Requirement 11.3). This module never computes a CRC: it imports neither
``taperescue.crc`` nor the reader's CRC routine, so the independent reader's
recomputation in ``verify_export`` is a real check.

Image layout:

* ``UEF File!\\0``, minor version 10, major version 0.
* Chunk ``0x0000``: origin text ``TapeRescue <version>`` with a NUL terminator.
* For each block ``0..final``: chunk ``0x0110`` carrier tone, then chunk
  ``0x0100`` holding the ``0x2A`` sync byte, the source ``header_raw``, the
  recorded header CRC, the selected payload and the recorded data CRC. CRCs
  are high byte first. An empty block has no payload and no data CRC.
* A trailing ``0x0110`` carrier tone.

Carrier lengths. The UEF 0.10 specification counts chunk ``0x0110`` in
cycles at twice the base frequency, i.e. 2400 Hz at 1200 baud
(fileformats.org, "UEF file format: Chunks", citing the specification draft
28 and the EmWare reader). The BBC MOS writes 5.1 s of leader before the
first block of a file and 0.9 s between blocks, so the counts are
5.1 x 2400 = 12240 and 0.9 x 2400 = 2160. These are the values in
``taperescue.fixtures.fsk.BBC_TIMING``. The design's 5100/1800 would be
2.125 s and 0.75 s in the specification's unit; see verification entry 9.4.

Source bytes for block ``n``:

* Header bytes and header CRC: the valid-header read with the lowest
  ``(capture_id, sample_offset)``, the same read the export gate checks.
* Data CRC: the recorded value from the first such read that is intact with
  the selected payload; otherwise the first complete read with a readable
  recorded data CRC (a reconstructed block's reads all carry the same
  recorded CRC, which the engine checks before reconstructing).
"""

from __future__ import annotations

import os
import struct
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from taperescue import __version__
from taperescue.engine.gate import ExportDecision, export_decision
from taperescue.models import BlockObservation, BlockResult, RecoveryResult
from taperescue.uef.verify import verify_export  # verify does not import this module

__all__ = [
    "CHUNK_CARRIER",
    "CHUNK_ORIGIN",
    "CHUNK_TAPE_DATA",
    "ExportRefused",
    "FIRST_CARRIER_CYCLES",
    "OTHER_CARRIER_CYCLES",
    "TRAILING_CARRIER_CYCLES",
    "build_uef",
    "export_uef",
]

UEF_MAGIC = b"UEF File!\x00"
UEF_MINOR, UEF_MAJOR = 10, 0
CHUNK_ORIGIN = 0x0000
CHUNK_TAPE_DATA = 0x0100
CHUNK_CARRIER = 0x0110
SYNC_BYTE = 0x2A

# Cycles of 2400 Hz (twice the 1200 Hz base frequency), per the UEF spec.
FIRST_CARRIER_CYCLES = 12240   # 5.1 s before block 0
OTHER_CARRIER_CYCLES = 2160    # 0.9 s before every later block
TRAILING_CARRIER_CYCLES = 4800  # 2.0 s after the last block (fixture trailer)


class ExportRefused(Exception):
    """The export gate, or the evidence for a CRC, does not allow export."""

    def __init__(self, blockers: Iterable[tuple[int, str]]) -> None:
        self.blockers: tuple[tuple[int, str], ...] = tuple(sorted(blockers))
        listed = ", ".join(f"block {n}: {reason}" for n, reason in self.blockers)
        super().__init__(f"export refused ({listed})" if listed else "export refused")


@dataclass(frozen=True)
class _BlockBytes:
    header_raw: bytes
    header_crc: int
    payload: bytes
    data_crc: int | None


def _chunk(chunk_id: int, data: bytes) -> bytes:
    return struct.pack("<HI", chunk_id, len(data)) + data


def _crc_bytes(value: int) -> bytes:
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


def _valid_header_reads(block: BlockResult) -> list[BlockObservation]:
    return sorted(
        (r for r in block.reads if r.header_crc.outcome == "valid" and r.header is not None),
        key=lambda r: (r.capture_id, r.sample_offset),
    )


def _recorded_data_crc(block: BlockResult, reads: list[BlockObservation]) -> int | None:
    for r in reads:
        if r.is_intact and r.payload == block.payload and r.data_crc is not None:
            return r.data_crc.recorded
    for r in reads:
        if (
            r.data_crc is not None
            and r.data_crc.outcome != "unreadable"
            and r.payload_state == "complete"
        ):
            return r.data_crc.recorded
    return None


def _block_bytes(block: BlockResult) -> _BlockBytes:
    reads = _valid_header_reads(block)
    if not reads or block.payload is None:
        raise ExportRefused([(block.block_no, "header_invalid" if not reads else "payload_missing")])
    source = reads[0]
    header_crc = source.header_crc.recorded
    assert header_crc is not None  # a valid header CRC always carries its value
    if not block.payload:
        return _BlockBytes(source.header_raw, header_crc, b"", None)
    data_crc = _recorded_data_crc(block, reads)
    if data_crc is None:
        raise ExportRefused([(block.block_no, "crc_unreadable")])
    return _BlockBytes(source.header_raw, header_crc, block.payload, data_crc)


def _tape_data(b: _BlockBytes) -> bytes:
    data = bytes([SYNC_BYTE]) + b.header_raw + _crc_bytes(b.header_crc)
    if b.data_crc is not None:
        data += b.payload + _crc_bytes(b.data_crc)
    return data


def build_uef(result: RecoveryResult, decision: ExportDecision | None = None) -> bytes:
    """Return the UEF image bytes, or raise :class:`ExportRefused`.

    Export is refused if ``decision`` (when given) or the gate's own decision
    on ``result`` is not permitted, so a stale decision cannot unlock export.
    """
    if decision is not None and not decision.permitted:
        raise ExportRefused(decision.blockers)
    gate = export_decision(result)
    if not gate.permitted:
        raise ExportRefused(gate.blockers)
    final = result.final_block
    assert final is not None  # the gate requires a final block
    by_no = {b.block_no: b for b in result.blocks}

    out = bytearray(UEF_MAGIC + bytes((UEF_MINOR, UEF_MAJOR)))
    out += _chunk(CHUNK_ORIGIN, f"TapeRescue {__version__}".encode("ascii") + b"\x00")
    for n in range(final + 1):
        cycles = FIRST_CARRIER_CYCLES if n == 0 else OTHER_CARRIER_CYCLES
        out += _chunk(CHUNK_CARRIER, struct.pack("<H", cycles))
        out += _chunk(CHUNK_TAPE_DATA, _tape_data(_block_bytes(by_no[n])))
    out += _chunk(CHUNK_CARRIER, struct.pack("<H", TRAILING_CARRIER_CYCLES))
    return bytes(out)


def _write_atomic(out: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def export_uef(result: RecoveryResult, decision: ExportDecision, out: Path) -> None:
    """Write ``result`` to ``out`` as a UEF image and verify it.

    Raises :class:`ExportRefused` before writing anything when export is not
    permitted. After writing (temporary file, then rename) the image is read
    back with the independent reader; if it does not match, the output is
    deleted and :class:`taperescue.uef.verify.ExportVerificationFailed` is
    raised.
    """
    data = build_uef(result, decision)
    out = Path(out)
    _write_atomic(out, data)
    verify_export(out, result)
