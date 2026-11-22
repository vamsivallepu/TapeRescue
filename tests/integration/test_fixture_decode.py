"""Decoder observations on the generated STARMAP fixtures (task 8).

These tests run the patched ``ScanTape`` through
:func:`taperescue.decoder_adapter.decode_capture` on the real fixture WAVs and
check the observations against ``reference_payloads.json`` and ``damage.json``.

Requirements: 2.1, 2.3, 2.4, 2.9, 3.5, 4.11, 5.6, 5.7.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from taperescue.decoder_adapter import DecoderError, decode_capture
from taperescue.fixtures.fsk import wav_header
from taperescue.models import BlockObservation, CaptureObservations

ROOT = Path(__file__).resolve().parents[2]
DECODER = ROOT / "build" / "decoder" / "ScanTape"
FIXTURES = ROOT / "fixtures" / "starmap"

pytestmark = pytest.mark.skipif(
    not (DECODER.is_file() and os.access(DECODER, os.X_OK)),
    reason=(
        f"patched decoder not built at {DECODER}; run scripts/build_decoder.sh "
        "to enable the fixture decode integration tests"
    ),
)

FINAL_FLAG = 0x80
DAMAGED_BLOCK = 6
CUT_BLOCK = 3
CUT_PAYLOAD_BYTES = 100  # whole payload bytes before the cut
CUT_EXTRA_BITS = 5  # the cut falls 5 bits into the next byte frame
SAMPLES_PER_BIT_NUM, SAMPLES_PER_BIT_DEN = 147, 4  # 36.75 samples per bit at 44.1 kHz
BITS_PER_FRAME = 10  # start bit, 8 data bits, stop bit
WAV_HEADER_BYTES = 44
BYTES_PER_SAMPLE = 2


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reference() -> dict:
    return _load("reference_payloads.json")


@pytest.fixture(scope="module")
def damage() -> dict:
    return _load("damage.json")


@pytest.fixture(scope="module")
def decode(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("decode")
    cache: dict[Path, CaptureObservations] = {}

    def _decode(wav: Path) -> CaptureObservations:
        if wav not in cache:
            cache[wav] = decode_capture(wav, decoder=DECODER, workdir=workdir)
        return cache[wav]

    return _decode


def _by_block(capture: CaptureObservations) -> dict[int, BlockObservation]:
    blocks: dict[int, BlockObservation] = {}
    for obs in capture.observations:
        assert obs.header is not None, f"observation at {obs.sample_offset} has no valid header"
        assert obs.header.block_no not in blocks, f"block {obs.header.block_no} read twice"
        blocks[obs.header.block_no] = obs
    return blocks


def _assert_matches_reference(obs: BlockObservation, ref_block: dict, ref: dict) -> None:
    n = ref_block["block_no"]
    assert obs.is_intact, f"block {n} is not intact"
    assert obs.header_crc.outcome == "valid"
    assert obs.header_crc.recorded == ref_block["header_crc"]
    assert obs.header_raw.hex() == ref_block["header_raw"]
    assert obs.header.filename == ref["filename"]
    assert obs.header.load_addr == ref["load_addr"]
    assert obs.header.exec_addr == ref["exec_addr"]
    assert obs.header.block_len == ref_block["block_len"]
    assert obs.header.flags == ref_block["flags"]
    assert obs.expected_len == obs.actual_len == ref_block["block_len"]
    assert obs.data_crc is not None and obs.data_crc.outcome == "valid"
    assert obs.data_crc.recorded == ref_block["data_crc"]
    assert obs.payload.hex() == ref_block["hex"], f"block {n} payload differs from reference"


def test_clean_fixture_decodes_to_reference(decode, reference):
    capture = decode(FIXTURES / "clean.wav")
    blocks = _by_block(capture)
    assert sorted(blocks) == list(range(8))
    for ref_block in reference["blocks"]:
        _assert_matches_reference(blocks[ref_block["block_no"]], ref_block, reference)
    assert [n for n, obs in blocks.items() if obs.header.flags & FINAL_FLAG] == [7]
    program = b"".join(blocks[n].payload for n in range(8))
    assert len(program) == reference["program_length"]


@pytest.mark.parametrize("side", ["a", "b"])
def test_damaged_fixture_block6_raw_observation(decode, reference, damage, side):
    capture = decode(FIXTURES / f"damaged_{side}.wav")
    blocks = _by_block(capture)
    assert sorted(blocks) == list(range(8))

    for ref_block in reference["blocks"]:
        if ref_block["block_no"] != DAMAGED_BLOCK:
            _assert_matches_reference(blocks[ref_block["block_no"]], ref_block, reference)

    ref6 = reference["blocks"][DAMAGED_BLOCK]
    obs = blocks[DAMAGED_BLOCK]
    assert obs.header_crc.outcome == "valid"
    assert obs.header_raw.hex() == ref6["header_raw"]
    assert obs.payload_state == "complete"
    assert obs.expected_len == obs.actual_len == ref6["block_len"]
    assert obs.data_crc is not None
    assert obs.data_crc.outcome == "invalid"
    assert obs.data_crc.recorded == ref6["data_crc"] == damage["data_crc"]
    assert not obs.is_intact

    original = bytes.fromhex(ref6["hex"])
    diff = {i: a ^ b for i, (a, b) in enumerate(zip(obs.payload, original)) if a != b}
    expected = {flip["offset"]: flip["mask"] for flip in damage[side]}
    assert diff == expected
    for flip in damage[side]:
        assert obs.payload[flip["offset"]] == flip["damaged"]
        assert original[flip["offset"]] == flip["original"]


def _cut_wav(clean: Path, out: Path, cut_sample: int) -> None:
    data = clean.read_bytes()
    assert data[36:40] == b"data", "clean.wav does not have the 44-byte header the cut expects"
    (data_bytes,) = struct.unpack("<I", data[40:44])
    keep = cut_sample * BYTES_PER_SAMPLE
    assert 0 < keep < data_bytes
    out.write_bytes(wav_header(keep) + data[WAV_HEADER_BYTES:WAV_HEADER_BYTES + keep])


def test_wav_cut_inside_block3_gives_truncated_observation(decode, reference, tmp_path):
    clean_obs = _by_block(decode(FIXTURES / "clean.wav"))[CUT_BLOCK]
    ref3 = reference["blocks"][CUT_BLOCK]
    # Byte frames before the payload, from the sync byte: sync (1), header_raw, header CRC (2).
    lead_bytes = 1 + len(bytes.fromhex(ref3["header_raw"])) + 2
    bits = (lead_bytes + CUT_PAYLOAD_BYTES) * BITS_PER_FRAME + CUT_EXTRA_BITS
    cut_sample = clean_obs.sample_offset + -(-bits * SAMPLES_PER_BIT_NUM // SAMPLES_PER_BIT_DEN)

    cut = tmp_path / "cut_block3.wav"
    _cut_wav(FIXTURES / "clean.wav", cut, cut_sample)
    blocks = _by_block(decode(cut))

    assert sorted(blocks) == list(range(CUT_BLOCK + 1)), "no block may follow the cut"
    for n in range(CUT_BLOCK):
        _assert_matches_reference(blocks[n], reference["blocks"][n], reference)

    obs = blocks[CUT_BLOCK]
    assert obs.header_crc.outcome == "valid"
    assert obs.expected_len == ref3["block_len"]
    assert obs.actual_len < obs.expected_len
    assert obs.actual_len == CUT_PAYLOAD_BYTES
    assert obs.payload_state == "truncated"
    assert len(obs.payload) == obs.actual_len
    assert obs.payload == bytes.fromhex(ref3["hex"])[: obs.actual_len], "payload must be unpadded"
    assert obs.data_crc is not None and obs.data_crc.outcome == "unreadable"
    assert obs.data_crc.recorded is None
    assert not obs.is_intact


def test_silent_wav_gives_no_blocks(decode, tmp_path):
    silent = tmp_path / "silent.wav"
    pcm = b"\0" * (44_100 * BYTES_PER_SAMPLE)  # 1 s of silence
    silent.write_bytes(wav_header(len(pcm)) + pcm)
    with pytest.raises(DecoderError) as info:
        decode(silent)
    assert info.value.code == "no_blocks"
    assert str(info.value) == "No BBC 1200-baud blocks were found in this capture"
