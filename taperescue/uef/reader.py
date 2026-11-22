"""Independent UEF tape image reader.

This module validates images written by the exporter, so it shares no code
with it: it imports nothing from ``taperescue`` (in particular not
``taperescue.crc`` or ``taperescue.uef.exporter``) and has its own bitwise
CRC-16/XMODEM. A bug in the table-driven CRC or in the exporter therefore
cannot hide in both components.

UEF container (UEF specification 0.10): optional gzip layer (possibly several
members), then the 10 bytes ``UEF File!\\0``, a minor and a major version
byte, then chunks of ``<id: u16 LE> <length: u32 LE> <data>`` to the end of
the file. Only chunk ``0x0100`` (implicit start/stop bit tape data) is read;
every other chunk is skipped by its length.

The data of all ``0x0100`` chunks, joined end to end in file order, is the
byte stream the cassette filing system wrote. It holds BBC cassette blocks:

======================  ======  ============================================
Field                   Size    Notes
======================  ======  ============================================
Sync                    1       ``0x2A``
Filename                1-10    then ``0x00``
Load address            4       little-endian
Execution address       4       little-endian
Block number            2       little-endian
Block length            2       little-endian
Block flags             1       bit 7 last block, bit 6 empty, bit 0 locked
Next-file address       4       little-endian
Header CRC              2       high byte first; filename to next address
Data                    length
Data CRC                2       high byte first; absent when length is 0
======================  ======  ============================================

Bytes outside a block that are not ``0x2A`` (for example the ``0xAA`` dummy
byte some MOS versions write at the start of a leader) are skipped. A block
is consumed using its length field even when its header CRC is wrong, and the
failure is reported in ``header_crc_ok``. A block cut short by the end of the
data, or a ``0x2A`` that is not followed by a terminated 1-10 byte filename,
raises :class:`UefFormatError`.
"""
from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["UefBlock", "UefFormatError", "parse_uef", "read_uef"]

UEF_MAGIC = b"UEF File!\x00"
GZIP_MAGIC = b"\x1f\x8b"
CHUNK_TAPE_DATA = 0x0100
SYNC_BYTE = 0x2A
MAX_FILENAME_LEN = 10
# Bytes after the filename terminator: load(4) exec(4) block_no(2) len(2) flags(1) next(4).
_HEADER_TAIL_LEN = 17


class UefFormatError(ValueError):
    """The file is not a UEF image or holds a malformed tape block."""


def _crc16_xmodem_bitwise(data: bytes) -> int:
    """CRC-16/XMODEM, one bit at a time: poly 0x1021, init 0, no reflection, no xorout."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class UefBlock:
    """One BBC cassette block found in a UEF image.

    ``header_raw`` is filename through next-file address, without the sync
    byte and header CRC. CRC fields hold the values stored in the file; the
    ``*_ok`` flags say whether they equal the CRC recomputed from the file
    bytes. An empty block has no data CRC, so ``data_crc_in_file`` is None
    and ``data_crc_ok`` is True.
    """

    header_raw: bytes
    header_crc_in_file: int
    payload: bytes
    data_crc_in_file: int | None
    header_crc_ok: bool
    data_crc_ok: bool

    def _tail(self) -> bytes:
        return self.header_raw[self.header_raw.index(0) + 1 :]

    @property
    def filename(self) -> str:
        return self.header_raw[: self.header_raw.index(0)].decode("latin-1")

    @property
    def load_addr(self) -> int:
        return struct.unpack_from("<I", self._tail(), 0)[0]

    @property
    def exec_addr(self) -> int:
        return struct.unpack_from("<I", self._tail(), 4)[0]

    @property
    def block_no(self) -> int:
        return struct.unpack_from("<H", self._tail(), 8)[0]

    @property
    def block_len(self) -> int:
        return struct.unpack_from("<H", self._tail(), 10)[0]

    @property
    def flags(self) -> int:
        return self._tail()[12]

    @property
    def next_addr(self) -> int:
        return struct.unpack_from("<I", self._tail(), 13)[0]


def _inflate(data: bytes) -> bytes:
    if data[:2] != GZIP_MAGIC:
        return data
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise UefFormatError(f"gzip layer is corrupt: {exc}") from exc


def _tape_stream(data: bytes) -> bytes:
    """Return the joined data of every 0x0100 chunk, in file order."""
    if data[: len(UEF_MAGIC)] != UEF_MAGIC:
        raise UefFormatError("missing 'UEF File!' signature")
    pos = len(UEF_MAGIC) + 2  # minor and major version bytes
    if len(data) < pos:
        raise UefFormatError("file ends inside the UEF header")
    stream = bytearray()
    while pos < len(data):
        if len(data) - pos < 6:
            raise UefFormatError(f"chunk header cut short at offset {pos}")
        chunk_id, length = struct.unpack_from("<HI", data, pos)
        start = pos + 6
        end = start + length
        if end > len(data):
            raise UefFormatError(
                f"chunk 0x{chunk_id:04X} at offset {pos} declares {length} bytes, "
                f"only {len(data) - start} remain"
            )
        if chunk_id == CHUNK_TAPE_DATA:
            stream += data[start:end]
        pos = end
    return bytes(stream)


def _parse_blocks(stream: bytes) -> list[UefBlock]:
    blocks: list[UefBlock] = []
    pos = 0
    n = len(stream)
    while pos < n:
        if stream[pos] != SYNC_BYTE:
            pos += 1
            continue
        sync_at = pos
        name_start = pos + 1
        nul = stream.find(b"\x00", name_start, name_start + MAX_FILENAME_LEN + 1)
        if nul < 0:
            raise UefFormatError(
                f"block at stream offset {sync_at}: no filename terminator within "
                f"{MAX_FILENAME_LEN} bytes"
            )
        if nul == name_start:
            raise UefFormatError(f"block at stream offset {sync_at}: empty filename")
        header_end = nul + 1 + _HEADER_TAIL_LEN
        if header_end + 2 > n:
            raise UefFormatError(f"block at stream offset {sync_at}: header cut short")
        header_raw = stream[name_start:header_end]
        header_crc = (stream[header_end] << 8) | stream[header_end + 1]
        block_len = struct.unpack_from("<H", stream, nul + 1 + 10)[0]
        data_start = header_end + 2
        if block_len == 0:
            payload = b""
            data_crc: int | None = None
            data_crc_ok = True
            pos = data_start
        else:
            data_end = data_start + block_len
            if data_end + 2 > n:
                raise UefFormatError(
                    f"block at stream offset {sync_at}: declares {block_len} data bytes "
                    f"plus CRC, only {n - data_start} remain"
                )
            payload = stream[data_start:data_end]
            data_crc = (stream[data_end] << 8) | stream[data_end + 1]
            data_crc_ok = _crc16_xmodem_bitwise(payload) == data_crc
            pos = data_end + 2
        blocks.append(
            UefBlock(
                header_raw=header_raw,
                header_crc_in_file=header_crc,
                payload=payload,
                data_crc_in_file=data_crc,
                header_crc_ok=_crc16_xmodem_bitwise(header_raw) == header_crc,
                data_crc_ok=data_crc_ok,
            )
        )
    return blocks


def parse_uef(data: bytes) -> list[UefBlock]:
    """Parse UEF image bytes (plain or gzip) into cassette blocks, in tape order."""
    return _parse_blocks(_tape_stream(_inflate(bytes(data))))


def read_uef(path: Path) -> list[UefBlock]:
    """Read a UEF file (plain or gzip) and return its cassette blocks, in tape order."""
    return parse_uef(Path(path).read_bytes())
