"""Unit tests for the independent UEF reader (task 9.3, Requirement 11.4).

UEF images are built by hand here; the exporter is not used. Test-side CRCs
come from ``taperescue.crc`` so they cross-check the reader's own bitwise CRC.
"""
from __future__ import annotations

import ast
import gzip
import struct
from pathlib import Path

import pytest

from taperescue.crc import crc16_xmodem
from taperescue.uef import reader
from taperescue.uef.reader import UefBlock, UefFormatError, parse_uef, read_uef

TEST_PAYLOAD = bytes((17 * i + 11 * i * i + 42) % 256 for i in range(256))


# --- hand-built UEF helpers ---------------------------------------------------

def header_raw(name: bytes, load: int, exe: int, block_no: int, length: int,
               flags: int, next_addr: int = 0) -> bytes:
    return name + b"\x00" + struct.pack("<IIHHBI", load, exe, block_no, length, flags, next_addr)


def tape_block(name: bytes, block_no: int, payload: bytes, flags: int,
               load: int = 0xFFFF0E00, exe: int = 0xFFFF8023,
               header_crc: int | None = None, data_crc: int | None = None) -> bytes:
    """Sync byte, header, header CRC (high first), data, data CRC (high first)."""
    hdr = header_raw(name, load, exe, block_no, len(payload), flags)
    hcrc = crc16_xmodem(hdr) if header_crc is None else header_crc
    out = b"\x2a" + hdr + struct.pack(">H", hcrc)
    if payload:
        dcrc = crc16_xmodem(payload) if data_crc is None else data_crc
        out += payload + struct.pack(">H", dcrc)
    return out


def chunk(chunk_id: int, data: bytes) -> bytes:
    return struct.pack("<HI", chunk_id, len(data)) + data


def uef(*chunks: bytes) -> bytes:
    return b"UEF File!\x00" + bytes([10, 0]) + b"".join(chunks)


def tone(cycles: int) -> bytes:
    return chunk(0x0110, struct.pack("<H", cycles))


BLOCK0 = TEST_PAYLOAD
BLOCK1 = bytes(range(38))


def two_block_image() -> bytes:
    return uef(
        chunk(0x0000, b"hand-built\x00"),
        tone(5100),
        chunk(0x0100, tape_block(b"STARMAP", 0, BLOCK0, 0x00)),
        tone(1800),
        chunk(0x0100, tape_block(b"STARMAP", 1, BLOCK1, 0x80)),
        tone(5300),
    )


# --- CRC ------------------------------------------------------------------------

def test_bitwise_crc_check_values():
    assert reader._crc16_xmodem_bitwise(b"123456789") == 0x31C3
    assert reader._crc16_xmodem_bitwise(TEST_PAYLOAD) == 0xAC93
    assert reader._crc16_xmodem_bitwise(b"") == 0


def test_bitwise_crc_agrees_with_table_crc():
    for data in (b"\x00", b"\xff" * 300, BLOCK1, header_raw(b"STARMAP", 0xFFFF0E00, 0xFFFF8023, 6, 256, 0)):
        assert reader._crc16_xmodem_bitwise(data) == crc16_xmodem(data)


# --- parsing ----------------------------------------------------------------------

def test_plain_uef_two_blocks():
    blocks = parse_uef(two_block_image())
    assert len(blocks) == 2
    b0, b1 = blocks
    assert b0.header_raw == header_raw(b"STARMAP", 0xFFFF0E00, 0xFFFF8023, 0, 256, 0x00)
    assert b0.payload == BLOCK0
    assert b0.header_crc_in_file == crc16_xmodem(b0.header_raw)
    assert b0.data_crc_in_file == 0xAC93
    assert b0.header_crc_ok and b0.data_crc_ok
    assert (b0.filename, b0.load_addr, b0.exec_addr, b0.block_no, b0.block_len, b0.flags, b0.next_addr) == (
        "STARMAP", 0xFFFF0E00, 0xFFFF8023, 0, 256, 0x00, 0)
    assert (b1.block_no, b1.block_len, b1.flags, b1.payload) == (1, 38, 0x80, BLOCK1)
    assert b1.header_crc_ok and b1.data_crc_ok
    assert b0.payload + b1.payload == BLOCK0 + BLOCK1


def test_crc_stored_high_byte_first():
    raw = tape_block(b"A", 0, b"\x01\x02", 0x80)
    blocks = parse_uef(uef(chunk(0x0100, raw)))
    crc = crc16_xmodem(b"\x01\x02")
    assert raw[-2:] == bytes([crc >> 8, crc & 0xFF])
    assert blocks[0].data_crc_in_file == crc and blocks[0].data_crc_ok


def test_gzip_uef_matches_plain():
    plain = two_block_image()
    assert parse_uef(gzip.compress(plain)) == parse_uef(plain)


def test_multi_member_gzip():
    plain = two_block_image()
    cut = 12 + len(chunk(0x0000, b"hand-built\x00")) + len(tone(5100)) + 50
    members = gzip.compress(plain[:cut]) + gzip.compress(plain[cut:])
    assert parse_uef(members) == parse_uef(plain)


def test_unknown_chunks_and_dummy_bytes_ignored():
    image = uef(
        chunk(0x9999, b"\x2a\x2a\x2a not tape data"),
        chunk(0x0112, struct.pack("<H", 100)),
        chunk(0x0100, b"\xaa" + tape_block(b"STARMAP", 0, BLOCK1, 0x80)),
        chunk(0x0003, b""),
    )
    blocks = parse_uef(image)
    assert len(blocks) == 1
    assert blocks[0].payload == BLOCK1 and blocks[0].data_crc_ok


def test_block_split_across_chunks():
    raw = tape_block(b"STARMAP", 0, BLOCK0, 0x80)
    image = uef(
        chunk(0x0100, raw[:5]),          # inside the filename
        chunk(0x0100, raw[5:30]),        # through the header CRC and into the data
        tone(10),
        chunk(0x0100, raw[30:]),
    )
    assert parse_uef(image) == parse_uef(uef(chunk(0x0100, raw)))


def test_payload_containing_sync_byte_is_not_resynced():
    payload = b"\x2a" * 20
    image = uef(chunk(0x0100, tape_block(b"X", 0, payload, 0x80)))
    blocks = parse_uef(image)
    assert len(blocks) == 1 and blocks[0].payload == payload


def test_bad_data_crc_detected():
    image = uef(chunk(0x0100, tape_block(b"STARMAP", 6, TEST_PAYLOAD, 0x00, data_crc=0x1234)))
    (b,) = parse_uef(image)
    assert b.data_crc_in_file == 0x1234
    assert not b.data_crc_ok
    assert b.header_crc_ok
    assert b.payload == TEST_PAYLOAD


def test_flipped_payload_bit_detected():
    raw = bytearray(tape_block(b"STARMAP", 6, TEST_PAYLOAD, 0x00))
    data_start = 1 + len(b"STARMAP") + 1 + 17 + 2
    raw[data_start + 17] ^= 0x04
    (b,) = parse_uef(uef(chunk(0x0100, bytes(raw))))
    assert b.data_crc_in_file == 0xAC93
    assert not b.data_crc_ok


def test_bad_header_crc_detected_and_block_still_parsed():
    image = uef(
        chunk(0x0100, tape_block(b"STARMAP", 0, BLOCK0, 0x00, header_crc=0xBEEF)),
        chunk(0x0100, tape_block(b"STARMAP", 1, BLOCK1, 0x80)),
    )
    b0, b1 = parse_uef(image)
    assert b0.header_crc_in_file == 0xBEEF
    assert not b0.header_crc_ok
    assert b0.data_crc_ok and b0.payload == BLOCK0
    assert b1.header_crc_ok and b1.block_no == 1


def test_empty_block_has_no_data_crc():
    image = uef(chunk(0x0100,
                      tape_block(b"STARMAP", 0, BLOCK0, 0x00)
                      + tape_block(b"STARMAP", 1, b"", 0xC0)))
    b0, b1 = parse_uef(image)
    assert b1.payload == b"" and b1.block_len == 0
    assert b1.data_crc_in_file is None and b1.data_crc_ok
    assert b1.header_crc_ok and b1.flags == 0xC0


def test_ten_character_filename():
    (b,) = parse_uef(uef(chunk(0x0100, tape_block(b"ABCDEFGHIJ", 0, b"\x00", 0x80))))
    assert b.filename == "ABCDEFGHIJ" and b.header_crc_ok and b.data_crc_ok


def test_no_tape_data_gives_no_blocks():
    assert parse_uef(uef(tone(5100))) == []
    assert parse_uef(uef()) == []


def test_read_uef_from_file(tmp_path: Path):
    path = tmp_path / "tape.uef"
    path.write_bytes(gzip.compress(two_block_image()))
    blocks = read_uef(path)
    assert [b.block_no for b in blocks] == [0, 1]
    assert all(isinstance(b, UefBlock) for b in blocks)


# --- malformed input ----------------------------------------------------------------

@pytest.mark.parametrize(
    "data, message",
    [
        (b"RIFF\x00\x00\x00\x00WAVE", "signature"),
        (b"UEF File!\x00\x0a", "header"),
        (uef(b"\x00\x01\x05"), "chunk header cut short"),
        (uef(struct.pack("<HI", 0x0100, 100) + b"\x2a"), "declares 100 bytes"),
        (uef(chunk(0x0100, tape_block(b"STARMAP", 0, BLOCK0, 0x80)[:-1])), "only"),
        (uef(chunk(0x0100, tape_block(b"STARMAP", 0, BLOCK0, 0x80)[:15])), "header cut short"),
        (uef(chunk(0x0100, b"\x2a" + b"ELEVENCHARS" + b"\x00" + bytes(19))), "terminator"),
        (uef(chunk(0x0100, b"\x2a\x00" + bytes(19))), "empty filename"),
        (b"\x1f\x8b\x08\x00garbage", "gzip"),
    ],
)
def test_malformed_input_rejected(data: bytes, message: str):
    with pytest.raises(UefFormatError, match=message):
        parse_uef(data)


# --- independence ---------------------------------------------------------------------

def test_reader_imports_nothing_from_taperescue():
    tree = ast.parse(Path(reader.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in reader"
            imported.append(node.module or "")
    assert imported, "expected some stdlib imports"
    assert not [m for m in imported if m == "taperescue" or m.startswith("taperescue.")]
