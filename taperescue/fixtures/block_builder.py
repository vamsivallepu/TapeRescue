"""Block_Builder: split a program into BBC Micro cassette blocks.

Each block on tape is laid out as follows (multi-byte numbers little-endian
unless noted):

======================  ======  ============================================
Field                   Size    Notes
======================  ======  ============================================
Sync                    1       ``0x2A``
Filename                1-10    then ``0x00``
Load address            4
Execution address       4
Block number            2
Block length            2
Block flags             1       bit 7 last block, bit 6 empty block
Next-file address       4
Header CRC              2       high byte first; filename to next address
Data                    length
Data CRC                2       high byte first; absent when length is 0
======================  ======  ============================================

Blocks hold 256 bytes except the last, which holds the remainder. Only the
last block has flag bit 7. Both CRCs come from :func:`taperescue.crc.crc16_xmodem`.

:func:`block_frames` lists every byte of a block with its role and its offset
within that role, so the damage injector can target payload bytes by payload
offset and leave every other byte alone.

This module must not import ``taperescue.decoder_adapter`` (Requirement 4.8).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Literal

from taperescue.crc import crc16_xmodem

__all__ = [
    "BLOCK_SIZE",
    "FLAG_EMPTY",
    "FLAG_LAST",
    "SYNC_BYTE",
    "ByteFrame",
    "FrameRole",
    "TapeBlock",
    "block_frames",
    "build_blocks",
    "encode_header",
]

BLOCK_SIZE = 256
SYNC_BYTE = 0x2A
FLAG_LAST = 0x80
FLAG_EMPTY = 0x40
MAX_FILENAME_LEN = 10
MAX_BLOCKS = 0x10000  # block number is a 16-bit field

FrameRole = Literal["sync", "header", "header_crc", "payload", "data_crc"]


@dataclass(frozen=True)
class ByteFrame:
    """One byte as written to tape.

    ``offset`` is the byte's position within its role: the payload offset for
    ``payload`` frames, the index into ``header_raw`` for ``header`` frames,
    0 (high byte) or 1 (low byte) for CRC frames, and 0 for the sync byte.
    """

    value: int
    role: FrameRole
    offset: int

    def __post_init__(self) -> None:
        if not 0 <= self.value <= 0xFF:
            raise ValueError(f"frame value must be a byte, got {self.value!r}")


@dataclass(frozen=True)
class TapeBlock:
    filename: str
    load_addr: int
    exec_addr: int
    block_no: int
    flags: int
    next_addr: int
    payload: bytes
    header_raw: bytes      # filename through next-file address, no sync or CRC
    header_crc: int
    data_crc: int | None   # None for an empty block

    @property
    def block_len(self) -> int:
        return len(self.payload)


def _check_u32(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit in 32 bits, got {value!r}")


def _encode_filename(filename: str) -> bytes:
    try:
        name = filename.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"filename must be ASCII, got {filename!r}") from exc
    if not 1 <= len(name) <= MAX_FILENAME_LEN:
        raise ValueError(
            f"filename must be 1 to {MAX_FILENAME_LEN} characters, got {len(name)}"
        )
    if any(b < 0x21 or b > 0x7E for b in name):
        raise ValueError(f"filename must be printable ASCII without spaces, got {filename!r}")
    return name


def encode_header(filename: str, load_addr: int, exec_addr: int, block_no: int,
                  block_len: int, flags: int, next_addr: int) -> bytes:
    """Return ``header_raw``: filename, NUL, then the 17 bytes of header fields."""
    name = _encode_filename(filename)
    _check_u32("load_addr", load_addr)
    _check_u32("exec_addr", exec_addr)
    _check_u32("next_addr", next_addr)
    if not 0 <= block_no <= 0xFFFF:
        raise ValueError(f"block_no must fit in 16 bits, got {block_no!r}")
    if not 0 <= block_len <= BLOCK_SIZE:
        raise ValueError(f"block_len must be 0 to {BLOCK_SIZE}, got {block_len!r}")
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"flags must be a byte, got {flags!r}")
    return name + b"\x00" + struct.pack(
        "<IIHHBI", load_addr, exec_addr, block_no, block_len, flags, next_addr
    )


def build_blocks(program: bytes, filename: str = "STARMAP",
                 load_addr: int = 0xFFFF0E00, exec_addr: int = 0xFFFF8023,
                 next_addr: int = 0) -> list[TapeBlock]:
    """Split ``program`` into cassette blocks numbered from 0.

    Every block except the last holds 256 bytes. The last block has flag bit
    7. An empty program gives one empty block with flag bits 7 and 6 and no
    data CRC.
    """
    program = bytes(program)
    chunks = [program[i:i + BLOCK_SIZE] for i in range(0, len(program), BLOCK_SIZE)] or [b""]
    if len(chunks) > MAX_BLOCKS:
        raise ValueError(f"program needs {len(chunks)} blocks, at most {MAX_BLOCKS} allowed")
    last = len(chunks) - 1
    blocks = []
    for block_no, payload in enumerate(chunks):
        flags = (FLAG_LAST if block_no == last else 0) | (FLAG_EMPTY if not payload else 0)
        header_raw = encode_header(filename, load_addr, exec_addr, block_no,
                                   len(payload), flags, next_addr)
        blocks.append(TapeBlock(
            filename=filename,
            load_addr=load_addr,
            exec_addr=exec_addr,
            block_no=block_no,
            flags=flags,
            next_addr=next_addr,
            payload=payload,
            header_raw=header_raw,
            header_crc=crc16_xmodem(header_raw),
            data_crc=crc16_xmodem(payload) if payload else None,
        ))
    return blocks


def _crc_frames(crc: int, role: FrameRole) -> list[ByteFrame]:
    return [ByteFrame(crc >> 8, role, 0), ByteFrame(crc & 0xFF, role, 1)]


def block_frames(block: TapeBlock) -> list[ByteFrame]:
    """Return every byte of ``block`` in tape order, tagged with role and offset."""
    frames = [ByteFrame(SYNC_BYTE, "sync", 0)]
    frames += [ByteFrame(b, "header", i) for i, b in enumerate(block.header_raw)]
    frames += _crc_frames(block.header_crc, "header_crc")
    if block.payload:
        if block.data_crc is None:
            raise ValueError(f"block {block.block_no} has a payload but no data CRC")
        frames += [ByteFrame(b, "payload", i) for i, b in enumerate(block.payload)]
        frames += _crc_frames(block.data_crc, "data_crc")
    return frames
