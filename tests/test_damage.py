"""Unit tests for the Damage_Injector (task 6.6, Requirements 5.1, 5.3, 5.4)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from taperescue.crc import crc16_xmodem
from taperescue.fixtures import damage
from taperescue.fixtures.block_builder import ByteFrame, block_frames, build_blocks
from taperescue.fixtures.damage import BitFlip, apply_damage

PROGRAM = bytes((31 * i + 7 * i * i + 5) % 256 for i in range(1800))
BLOCKS = build_blocks(PROGRAM)
BLOCK6 = BLOCKS[6]
FRAMES6 = block_frames(BLOCK6)
FLIPS_A = [BitFlip(6, 17, 2), BitFlip(6, 143, 5)]


def payload_of(frames: list[ByteFrame]) -> bytes:
    return bytes(f.value for f in frames if f.role == "payload")


def test_only_targeted_payload_bits_change() -> None:
    damaged = apply_damage(FRAMES6, 6, FLIPS_A)
    assert len(damaged) == len(FRAMES6)
    changed = [i for i, (a, b) in enumerate(zip(FRAMES6, damaged)) if a != b]
    assert [(FRAMES6[i].role, FRAMES6[i].offset) for i in changed] == [
        ("payload", 17), ("payload", 143)
    ]
    assert damaged[changed[0]].value == FRAMES6[changed[0]].value ^ 0x04
    assert damaged[changed[1]].value == FRAMES6[changed[1]].value ^ 0x20
    for i in changed:
        assert bin(damaged[i].value ^ FRAMES6[i].value).count("1") == 1


def test_roles_offsets_and_order_unchanged() -> None:
    damaged = apply_damage(FRAMES6, 6, FLIPS_A)
    assert [(f.role, f.offset) for f in damaged] == [(f.role, f.offset) for f in FRAMES6]


def test_crc_header_and_sync_frames_unchanged_and_payload_fails_crc() -> None:
    damaged = apply_damage(FRAMES6, 6, FLIPS_A)
    for before, after in zip(FRAMES6, damaged):
        if before.role != "payload":
            assert after == before
    data_crc = [f.value for f in damaged if f.role == "data_crc"]
    assert data_crc == [BLOCK6.data_crc >> 8, BLOCK6.data_crc & 0xFF]
    assert crc16_xmodem(payload_of(damaged)) != BLOCK6.data_crc


def test_input_frames_not_modified() -> None:
    snapshot = list(FRAMES6)
    apply_damage(FRAMES6, 6, FLIPS_A)
    assert FRAMES6 == snapshot


def test_no_flips_returns_equal_copy() -> None:
    damaged = apply_damage(FRAMES6, 6, [])
    assert damaged == FRAMES6
    assert damaged is not FRAMES6


def test_short_last_block_flip() -> None:
    last = BLOCKS[-1]
    frames = block_frames(last)
    damaged = apply_damage(frames, last.block_no, [BitFlip(last.block_no, last.block_len - 1, 7)])
    assert payload_of(damaged)[-1] == last.payload[-1] ^ 0x80


@pytest.mark.parametrize("offset", [256, 257, 300])
def test_rejects_offset_outside_payload(offset: int) -> None:
    # Offsets 256 and 257 would be the data CRC bytes if counted from the payload start.
    with pytest.raises(ValueError, match="only payload bytes"):
        apply_damage(FRAMES6, 6, [BitFlip(6, offset, 0)])


def test_rejects_flip_on_empty_block() -> None:
    empty = build_blocks(b"")[0]
    with pytest.raises(ValueError, match="only payload bytes"):
        apply_damage(block_frames(empty), 0, [BitFlip(0, 0, 0)])


def test_rejects_repeated_offset() -> None:
    with pytest.raises(ValueError, match="more than once"):
        apply_damage(FRAMES6, 6, [BitFlip(6, 64, 0), BitFlip(6, 64, 3)])
    with pytest.raises(ValueError, match="more than once"):
        apply_damage(FRAMES6, 6, [BitFlip(6, 64, 0), BitFlip(6, 64, 0)])


def test_rejects_unknown_block() -> None:
    with pytest.raises(ValueError, match="block 5"):
        apply_damage(FRAMES6, 6, [BitFlip(5, 17, 2)])


@pytest.mark.parametrize("bit", [-1, 8, 0x30, 255])
def test_rejects_bit_out_of_range_or_mask(bit: int) -> None:
    # A caller passing a multi-bit mask such as 0x30 or 0xFF instead of an
    # index lands outside 0..7 and is rejected.
    with pytest.raises(ValueError, match="single bit index"):
        BitFlip(6, 0, bit)


@pytest.mark.parametrize("field,args", [
    ("bit", (6, 0, True)),
    ("bit", (6, 0, 1.0)),
    ("offset", (6, "17", 0)),
    ("block_no", (None, 17, 0)),
])
def test_rejects_non_integer_fields(field: str, args: tuple) -> None:
    with pytest.raises(ValueError, match=field):
        BitFlip(*args)


def test_rejects_negative_offset() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        BitFlip(6, -1, 0)


def test_mask_is_single_bit() -> None:
    assert [BitFlip(0, 0, b).mask for b in range(8)] == [1 << b for b in range(8)]


def test_does_not_import_decoder_adapter() -> None:
    tree = ast.parse(Path(damage.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any("decoder_adapter" in n for n in names)
