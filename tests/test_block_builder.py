"""Unit tests for the Block_Builder (task 6.3, Requirements 4.3, 4.4, 4.5)."""
from __future__ import annotations

import ast
import struct
from pathlib import Path

import pytest

from taperescue.crc import crc16_xmodem
from taperescue.fixtures import block_builder
from taperescue.fixtures.block_builder import (
    ByteFrame,
    TapeBlock,
    block_frames,
    build_blocks,
    encode_header,
)
from taperescue.uef.reader import parse_uef

PROGRAM_1800 = bytes((17 * i + 11 * i * i + 42) % 256 for i in range(1800))


def frames_to_bytes(frames: list[ByteFrame]) -> bytes:
    return bytes(f.value for f in frames)


def uef_from_blocks(blocks: list[TapeBlock]) -> bytes:
    out = bytearray(b"UEF File!\x00" + bytes([10, 0]))
    for block in blocks:
        data = frames_to_bytes(block_frames(block))
        out += struct.pack("<HI", 0x0100, len(data)) + data
    return bytes(out)


def test_eight_blocks_with_256_bytes_except_last() -> None:
    blocks = build_blocks(PROGRAM_1800)
    assert [b.block_no for b in blocks] == list(range(8))
    assert [len(b.payload) for b in blocks] == [256] * 7 + [1800 - 7 * 256]
    assert b"".join(b.payload for b in blocks) == PROGRAM_1800
    assert all(b.filename == "STARMAP" for b in blocks)


def test_final_flag_only_on_last_block() -> None:
    blocks = build_blocks(PROGRAM_1800)
    assert [b.flags for b in blocks] == [0] * 7 + [0x80]


def test_exact_multiple_of_256_has_no_extra_block() -> None:
    blocks = build_blocks(bytes(512))
    assert [len(b.payload) for b in blocks] == [256, 256]
    assert blocks[-1].flags == 0x80


def test_header_layout() -> None:
    block = build_blocks(PROGRAM_1800)[6]
    expected = b"STARMAP\x00" + struct.pack("<IIHHBI", 0xFFFF0E00, 0xFFFF8023, 6, 256, 0, 0)
    assert block.header_raw == expected
    assert len(block.header_raw) == len("STARMAP") + 1 + 17


def test_crcs_come_from_crc16_xmodem() -> None:
    for block in build_blocks(PROGRAM_1800):
        assert block.header_crc == crc16_xmodem(block.header_raw)
        assert block.data_crc == crc16_xmodem(block.payload)


def test_frames_roles_offsets_and_crc_byte_order() -> None:
    block = build_blocks(PROGRAM_1800)[6]
    frames = block_frames(block)
    hlen = len(block.header_raw)
    assert frames[0] == ByteFrame(0x2A, "sync", 0)
    assert [f.role for f in frames[1:1 + hlen]] == ["header"] * hlen
    assert frames[1 + hlen] == ByteFrame(block.header_crc >> 8, "header_crc", 0)
    assert frames[2 + hlen] == ByteFrame(block.header_crc & 0xFF, "header_crc", 1)
    payload = [f for f in frames if f.role == "payload"]
    assert [f.offset for f in payload] == list(range(256))
    assert bytes(f.value for f in payload) == block.payload
    assert frames[-2:] == [ByteFrame(block.data_crc >> 8, "data_crc", 0),
                           ByteFrame(block.data_crc & 0xFF, "data_crc", 1)]
    assert len(frames) == 1 + hlen + 2 + 256 + 2


def test_empty_program_gives_one_empty_block_without_data_crc() -> None:
    [block] = build_blocks(b"")
    assert block.flags == 0x80 | 0x40
    assert block.payload == b"" and block.data_crc is None
    roles = {f.role for f in block_frames(block)}
    assert roles == {"sync", "header", "header_crc"}


def test_independent_reader_parses_built_blocks() -> None:
    blocks = build_blocks(PROGRAM_1800)
    parsed = parse_uef(uef_from_blocks(blocks))
    assert len(parsed) == 8
    for built, read in zip(blocks, parsed):
        assert read.header_raw == built.header_raw
        assert read.payload == built.payload
        assert read.header_crc_in_file == built.header_crc
        assert read.data_crc_in_file == built.data_crc
        assert read.header_crc_ok and read.data_crc_ok
        assert (read.filename, read.load_addr, read.exec_addr) == ("STARMAP", 0xFFFF0E00, 0xFFFF8023)
        assert (read.block_no, read.block_len, read.flags) == (built.block_no, len(built.payload), built.flags)


@pytest.mark.parametrize("filename", ["", "ELEVENCHARS", "BAD NAME", "N\x00L", "STÄR"])
def test_invalid_filenames_rejected(filename: str) -> None:
    with pytest.raises(ValueError):
        build_blocks(b"x", filename=filename)


def test_out_of_range_fields_rejected() -> None:
    with pytest.raises(ValueError):
        build_blocks(b"x", load_addr=1 << 32)
    with pytest.raises(ValueError):
        encode_header("A", 0, 0, 0, 257, 0, 0)
    with pytest.raises(ValueError):
        encode_header("A", 0, 0, 0x10000, 1, 0, 0)


def test_module_does_not_import_decoder_adapter() -> None:
    tree = ast.parse(Path(block_builder.__file__).read_text())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any("decoder_adapter" in name for name in names)
