"""Tests for the FSK_Modulator (task 6.5, Requirements 4.7, 4.8, 4.9)."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import random
import struct
import subprocess
from pathlib import Path

import pytest

from taperescue.fixtures import fsk
from taperescue.fixtures.block_builder import block_frames, build_blocks
from taperescue.fixtures.fsk import (
    BBC_TIMING,
    ByteFrames,
    Carrier,
    Gap,
    bit_start_sample,
    modulate,
    pcm_samples,
    segment_start_samples,
    tape_stream,
)
from taperescue.wavcheck import inspect_wav_header

REPO = Path(__file__).resolve().parents[1]
DECODER = REPO / "build" / "decoder" / "ScanTape"
PROGRAM_1800 = random.Random(65).randbytes(1800)


def samples_of(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def classify_cells(samples: list[int], n_cells: int) -> list[int | None]:
    """Recover bits from cells: count positive-to-negative zero crossings."""
    bits = []
    for k in range(n_cells):
        cell = samples[bit_start_sample(k):bit_start_sample(k + 1)]
        if not any(cell):
            bits.append(None)
            continue
        cycles = sum(1 for a, b in zip(cell, cell[1:]) if a > 0 and b <= 0)
        bits.append({1: 0, 2: 1}[cycles])
    return bits


def test_bit_boundaries_use_integer_floor():
    assert [bit_start_sample(k) for k in range(5)] == [0, 36, 73, 110, 147]
    assert all(bit_start_sample(k) == math.floor(k * 147 / 4) for k in range(10_000))
    # 1200 cells are exactly one second.
    assert bit_start_sample(1200) == 44_100


def test_sine_table_is_fixed_int16_at_amplitude_16000():
    assert len(fsk.SINE_TABLE) == 256
    assert fsk.SINE_TABLE == tuple(round(16000 * math.sin(2 * math.pi * i / 256)) for i in range(256))
    assert max(fsk.SINE_TABLE) == 16000 and min(fsk.SINE_TABLE) == -16000


def test_wav_header_is_44k1_16bit_mono_pcm():
    wav = modulate([Carrier(4), ByteFrames([0x2A])])
    assert wav[:4] == b"RIFF" and wav[8:16] == b"WAVEfmt " and wav[36:40] == b"data"
    assert len(wav) == 44 + 2 * bit_start_sample(2 + 10)
    info = inspect_wav_header(wav[:64], len(wav))
    assert (info.format_tag, info.sample_rate, info.bits_per_sample, info.channels) == (1, 44100, 16, 1)
    assert info.data_bytes == len(wav) - 44


def test_zero_is_one_1200hz_cycle_and_one_is_two_2400hz_cycles():
    samples = samples_of(pcm_samples([ByteFrames([0x00, 0xFF])]))
    bits = classify_cells(samples, 20)
    assert bits == [0] + [0] * 8 + [1] + [0] + [1] * 8 + [1]
    # Every cell starts at phase 0, so tone changes happen at a zero crossing.
    assert all(samples[bit_start_sample(k)] == 0 for k in range(20))


def test_byte_frame_is_start_bit_data_lsb_first_stop_bit():
    value = 0b1100_1010
    bits = classify_cells(samples_of(pcm_samples([ByteFrames([value])])), 10)
    assert bits == [0, 0, 1, 0, 1, 0, 0, 1, 1, 1]


def test_carrier_and_gap_lengths():
    stream = [Gap(3), Carrier(8), Gap(1)]
    samples = samples_of(pcm_samples(stream))
    assert len(samples) == bit_start_sample(3 + 4 + 1)
    assert classify_cells(samples, 8) == [None] * 3 + [1] * 4 + [None]
    assert segment_start_samples(stream) == (0, bit_start_sample(3), bit_start_sample(7), bit_start_sample(8))


def test_byte_frames_accept_block_builder_frames():
    frames = block_frames(build_blocks(b"\x01\x02\x03")[0])
    assert ByteFrames(frames).values == tuple(f.value for f in frames)


@pytest.mark.parametrize("bad", [
    lambda: Carrier(3),
    lambda: Carrier(-2),
    lambda: Gap(-1),
    lambda: ByteFrames([256]),
    lambda: ByteFrames([-1]),
    lambda: tape_stream([]),
])
def test_invalid_segments_are_rejected(bad):
    with pytest.raises(ValueError):
        bad()


def test_timing_table_matches_bbc_and_decoder_values():
    t = BBC_TIMING
    assert t.first_lead_cycles == 12240      # 5.1 s of 2400 Hz
    assert t.other_lead_cycles == 2160       # 0.9 s
    assert t.trailer_cycles == 4800          # 2.0 s, decoder needs >= 0.5 s
    assert t.first_prelude_cycles == 4
    assert t.dummy_byte == 0xAA
    # Decoder minimums from AtomTapeUtils bbmTiming: lead 2.0 s, other 0.2 s, trailer 0.5 s.
    assert t.first_lead_cycles >= 2.0 * 2400
    assert t.other_lead_cycles >= 0.2 * 2400
    assert t.trailer_cycles >= 0.5 * 2400


def test_tape_stream_layout():
    blocks = [block_frames(b) for b in build_blocks(bytes(300))]
    stream = tape_stream(blocks)
    t = BBC_TIMING
    assert stream == [
        Gap(t.leading_gap_bits),
        Carrier(t.first_prelude_cycles), ByteFrames([t.dummy_byte]), Carrier(t.first_lead_cycles),
        ByteFrames(blocks[0]),
        Carrier(t.other_lead_cycles),
        ByteFrames(blocks[1]),
        Carrier(t.trailer_cycles), Gap(t.trailing_gap_bits),
    ]


def test_output_is_deterministic():
    blocks = [block_frames(b) for b in build_blocks(PROGRAM_1800)]
    first = modulate(tape_stream(blocks))
    second = modulate(tape_stream([block_frames(b) for b in build_blocks(PROGRAM_1800)]))
    assert first == second


def test_module_does_not_import_or_run_the_decoder():
    source = Path(fsk.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(m.startswith("taperescue") for m in imported)
    assert not imported & {"subprocess", "os"}
    assert "ScanTape" not in source


@pytest.mark.skipif(not DECODER.exists(), reason=f"decoder not built: {DECODER} is missing")
def test_patched_decoder_reads_every_block_intact(tmp_path):
    """Integration: modulate an 8-block program and decode it with ScanTape -bbm -json."""
    blocks = build_blocks(PROGRAM_1800)
    stream = tape_stream([block_frames(b) for b in blocks])
    wav = modulate(stream)
    wav_path = tmp_path / "dummy.wav"
    wav_path.write_bytes(wav)
    out = tmp_path / "obs.json"
    capture_id = hashlib.sha256(wav).hexdigest()
    result = subprocess.run(
        [str(DECODER), str(wav_path), "-bbm", "-json", str(out), "-capture-id", capture_id],
        capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc["capture_id"] == capture_id
    assert len(doc["blocks"]) == len(blocks) == 8

    starts = segment_start_samples(stream)
    sync_samples = [starts[i] for i, s in enumerate(stream)
                    if isinstance(s, ByteFrames) and len(s.values) > 1]
    for obs, block, sync in zip(doc["blocks"], blocks, sync_samples):
        assert obs["header_crc"] == {"outcome": "valid", "recorded": block.header_crc}
        assert obs["data_crc"] == {"outcome": "valid", "recorded": block.data_crc}
        assert bytes.fromhex(obs["header_raw"]) == block.header_raw
        assert bytes.fromhex(obs["payload"]) == block.payload
        assert obs["expected_len"] == obs["actual_len"] == block.block_len
        assert obs["header"]["block_no"] == block.block_no
        assert obs["header"]["flags"] == block.flags
        assert abs(obs["sample_offset"] - sync) <= 37
    assert doc["blocks"][-1]["header"]["flags"] & 0x80
    assert b"".join(bytes.fromhex(o["payload"]) for o in doc["blocks"]) == PROGRAM_1800
