"""Clean fixture to UEF, end to end (task 9.6).

``clean.wav`` -> patched ``ScanTape`` (via :func:`decode_capture`) ->
:func:`recover` -> :func:`export_decision` -> :func:`export_uef`, then the
image is read back with the independent reader
(:mod:`taperescue.uef.reader`, which has its own bitwise CRC).

Two independent sources are checked against the file: the CRCs recorded in
the clean capture's observations (what the decoder read from the WAV), and
``reference_payloads.json`` (written by the fixture encoder, not the decoder).

Requirements: 4.11, 11.4, 11.5.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from taperescue.decoder_adapter import decode_capture
from taperescue.engine import recover
from taperescue.engine.gate import ExportDecision, export_decision
from taperescue.models import BlockObservation, CaptureObservations, RecoveryResult
from taperescue.uef.exporter import export_uef
from taperescue.uef.reader import UefBlock, read_uef

ROOT = Path(__file__).resolve().parents[2]
DECODER = ROOT / "build" / "decoder" / "ScanTape"
FIXTURES = ROOT / "fixtures" / "starmap"
CLEAN_WAV = FIXTURES / "clean.wav"

pytestmark = pytest.mark.skipif(
    not (DECODER.is_file() and os.access(DECODER, os.X_OK)),
    reason=(
        f"patched decoder not built at {DECODER}; run scripts/build_decoder.sh "
        "to enable the clean-fixture UEF integration test"
    ),
)

BLOCK_COUNT = 8
FINAL_BLOCK = 7
FINAL_FLAG = 0x80


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Run:
    wav_sha_before: str
    wav_sha_after: str
    capture: CaptureObservations
    result: RecoveryResult
    decision: ExportDecision
    uef_path: Path
    uef_blocks: list[UefBlock]


@pytest.fixture(scope="module")
def reference() -> dict:
    return json.loads((FIXTURES / "reference_payloads.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> Run:
    workdir = tmp_path_factory.mktemp("clean_uef")
    before = _sha256(CLEAN_WAV)
    capture = decode_capture(CLEAN_WAV, decoder=DECODER, workdir=workdir)
    result = recover([capture])
    decision = export_decision(result)
    uef_path = workdir / "clean.uef"
    if decision.permitted:
        export_uef(result, decision, uef_path)
    uef_blocks = read_uef(uef_path) if uef_path.exists() else []
    after = _sha256(CLEAN_WAV)
    return Run(before, after, capture, result, decision, uef_path, uef_blocks)


def _observations_by_block(capture: CaptureObservations) -> dict[int, BlockObservation]:
    by_no: dict[int, BlockObservation] = {}
    for obs in capture.observations:
        assert obs.header is not None
        assert obs.header.block_no not in by_no, f"block {obs.header.block_no} read twice"
        by_no[obs.header.block_no] = obs
    return by_no


def test_input_wav_is_unchanged(run, reference):
    assert run.wav_sha_before == reference["wav_sha256"]["clean.wav"]
    assert run.wav_sha_after == run.wav_sha_before
    assert run.capture.capture_id == run.wav_sha_before


def test_eight_intact_blocks_and_export_permitted(run):
    assert run.result.final_block == FINAL_BLOCK
    assert [b.block_no for b in run.result.blocks] == list(range(BLOCK_COUNT))
    assert [b.status for b in run.result.blocks] == ["Intact"] * BLOCK_COUNT
    assert run.decision.permitted, f"export refused: {run.decision.blockers}"
    assert run.decision.blockers == ()
    assert run.decision.reconstructed_blocks == ()
    assert run.uef_path.is_file()


def test_uef_crcs_equal_recorded_and_reference_crcs(run, reference):
    observed = _observations_by_block(run.capture)
    assert sorted(observed) == list(range(BLOCK_COUNT))
    assert [fb.block_no for fb in run.uef_blocks] == list(range(BLOCK_COUNT))

    for fb, ref in zip(run.uef_blocks, reference["blocks"], strict=True):
        n = ref["block_no"]
        obs = observed[n]
        assert obs.is_intact, f"block {n} observation is not intact"

        # Header: bytes and CRC from the capture, which equal the encoder's values.
        assert fb.header_raw == obs.header_raw
        assert fb.header_raw.hex() == ref["header_raw"]
        assert fb.header_crc_in_file == obs.header_crc.recorded == ref["header_crc"]
        assert fb.header_crc_ok, f"block {n}: header CRC does not recompute from file bytes"

        # Data: payload and CRC from the capture, which equal the encoder's values.
        assert fb.payload.hex() == ref["hex"]
        assert fb.payload == obs.payload
        assert obs.data_crc is not None
        assert fb.data_crc_in_file == obs.data_crc.recorded == ref["data_crc"]
        assert fb.data_crc_ok, f"block {n}: data CRC does not recompute from file bytes"

        assert fb.filename == reference["filename"]
        assert fb.load_addr == reference["load_addr"]
        assert fb.exec_addr == reference["exec_addr"]
        assert fb.block_len == ref["block_len"] == len(fb.payload)
        assert fb.flags == ref["flags"]


def test_concatenated_payload_equals_reference_program(run, reference):
    program = b"".join(fb.payload for fb in run.uef_blocks)
    assert len(program) == reference["program_length"]
    assert hashlib.sha256(program).hexdigest() == reference["program_sha256"]
    assert program == run.result.program_payload()


def test_final_flag_only_on_block_7(run):
    flagged = [fb.block_no for fb in run.uef_blocks if fb.flags & FINAL_FLAG]
    assert flagged == [FINAL_BLOCK]
