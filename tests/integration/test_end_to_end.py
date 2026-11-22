"""End-to-end STARMAP recovery from the two damaged fixtures (task 16.2).

``damaged_a.wav`` and ``damaged_b.wav`` each fail block 6 at two bytes, at
offsets disjoint between the two (``damage.json``: A at 17 and 143, B at 64
and 201). Both are decoded with the real patched ``ScanTape`` through
:func:`decode_capture`, and the CLI is also run as a subprocess
(``python -m taperescue.cli``).

Checked here:

* each damaged capture alone gives block 6 ``CRC failed`` and a refused
  export (14.1);
* the intact-only merge leaves block 6 unrecovered (14.2);
* full recovery gives block 6 ``Reconstructed · CRC match`` and every other
  block ``Intact``, with each damaged byte taken from the other capture (14.3);
* the exported UEF, read back with the independent reader, equals
  ``reference_payloads.json`` byte for byte, carries the CRCs recorded in the
  damaged captures, and is byte-identical to the image exported from
  ``clean.wav`` (14.4);
* swapping the input order gives byte-identical report and image;
* the input files' digests are unchanged (10.7);
* CLI failure paths exit non-zero with ``error: <reason>`` (16.3).

Expected values come from ``damage.json`` and ``reference_payloads.json``,
which the fixture encoder writes independently of the decoder.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from taperescue.decoder_adapter import decode_capture
from taperescue.engine import recover
from taperescue.engine.gate import export_decision
from taperescue.engine.report import build_report, dumps_report
from taperescue.models import BlockObservation, CaptureObservations, RecoverOptions
from taperescue.uef.exporter import ExportRefused, build_uef, export_uef
from taperescue.uef.reader import read_uef

ROOT = Path(__file__).resolve().parents[2]
DECODER = ROOT / "build" / "decoder" / "ScanTape"
FIXTURES = ROOT / "fixtures" / "starmap"
CLEAN_WAV = FIXTURES / "clean.wav"
DAMAGED = {"a": FIXTURES / "damaged_a.wav", "b": FIXTURES / "damaged_b.wav"}
REFERENCE_JSON = FIXTURES / "reference_payloads.json"
DAMAGE_JSON = FIXTURES / "damage.json"
INPUTS = (CLEAN_WAV, DAMAGED["a"], DAMAGED["b"], REFERENCE_JSON, DAMAGE_JSON)

pytestmark = pytest.mark.skipif(
    not (DECODER.is_file() and os.access(DECODER, os.X_OK)),
    reason=(
        f"patched decoder not built at {DECODER}; run scripts/build_decoder.sh "
        "to enable the end-to-end recovery tests"
    ),
)

BLOCK_COUNT = 8
DAMAGED_BLOCK = 6
RECONSTRUCTED = "Reconstructed · CRC match"
ORIGINAL_DATA_CRC = 35417  # 0x8A59, block 6 as recorded on tape


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _block(result, n):
    return next(b for b in result.blocks if b.block_no == n)


def _observation(capture: CaptureObservations, n: int) -> BlockObservation:
    found = [o for o in capture.observations if o.header is not None and o.header.block_no == n]
    assert len(found) == 1, f"capture {capture.capture_id[:12]}: block {n} read {len(found)} times"
    return found[0]


# --- shared runs ----------------------------------------------------------------

@dataclass(frozen=True)
class Decoded:
    digests_before: dict[Path, str]
    a: CaptureObservations
    b: CaptureObservations
    clean: CaptureObservations


@pytest.fixture(scope="module")
def reference() -> dict:
    return json.loads(REFERENCE_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def damage() -> dict:
    return json.loads(DAMAGE_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def decoded(tmp_path_factory) -> Decoded:
    before = {p: _sha256(p) for p in INPUTS}
    workdir = tmp_path_factory.mktemp("e2e_decode")
    caps = {
        name: decode_capture(path, decoder=DECODER, workdir=workdir)
        for name, path in {"a": DAMAGED["a"], "b": DAMAGED["b"], "clean": CLEAN_WAV}.items()
    }
    return Decoded(before, caps["a"], caps["b"], caps["clean"])


@pytest.fixture(scope="module")
def clean_uef(decoded) -> bytes:
    """Image exported from ``clean.wav`` alone (the task 9.6 path)."""
    result = recover([decoded.clean])
    return build_uef(result, export_decision(result))


@pytest.fixture(scope="module")
def full(decoded):
    result = recover([decoded.a, decoded.b])
    return result, export_decision(result)


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "TAPERESCUE_DECODER"}
    return subprocess.run(
        [sys.executable, "-m", "taperescue.cli", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
    )


# --- decoder observations -------------------------------------------------------

def test_capture_ids_are_the_fixture_digests(decoded, damage):
    assert decoded.a.capture_id == damage["wav_sha256"]["damaged_a.wav"]
    assert decoded.b.capture_id == damage["wav_sha256"]["damaged_b.wav"]


@pytest.mark.parametrize("which", ["a", "b"])
def test_block6_is_complete_with_original_recorded_crc(decoded, damage, reference, which):
    obs = _observation(getattr(decoded, which), DAMAGED_BLOCK)
    ref = reference["blocks"][DAMAGED_BLOCK]
    assert obs.header_crc.outcome == "valid"
    assert obs.payload_state == "complete"
    assert obs.actual_len == obs.expected_len == 256
    assert obs.data_crc is not None and obs.data_crc.outcome == "invalid"
    assert obs.data_crc.recorded == damage["data_crc"] == ref["data_crc"] == ORIGINAL_DATA_CRC
    original = bytes.fromhex(ref["hex"])
    diffs = {i: (original[i], obs.payload[i]) for i in range(256) if obs.payload[i] != original[i]}
    assert diffs == {d["offset"]: (d["original"], d["damaged"]) for d in damage[which]}


# --- 14.1: each damaged capture alone ----------------------------------------------

@pytest.mark.parametrize("which", ["a", "b"])
def test_single_damaged_capture_fails_block6_and_refuses_export(decoded, which):
    result = recover([getattr(decoded, which)])
    decision = export_decision(result)

    statuses = {b.block_no: b.status for b in result.blocks}
    assert statuses == {n: ("CRC failed" if n == DAMAGED_BLOCK else "Intact")
                        for n in range(BLOCK_COUNT)}
    block6 = _block(result, DAMAGED_BLOCK)
    assert block6.reason == "single_read"
    assert block6.payload is None and block6.reconstruction is None
    assert result.program_payload() is None

    assert not decision.permitted
    assert decision.blockers == ((DAMAGED_BLOCK, "single_read"),)
    with pytest.raises(ExportRefused):
        build_uef(result, decision)


# --- 14.2: intact-only merge ---------------------------------------------------------

def test_intact_only_merge_leaves_block6_unrecovered(decoded):
    result = recover([decoded.a, decoded.b], RecoverOptions(reconstruct=False))
    decision = export_decision(result)

    assert result.anchors == (0, 1, 2, 3, 4, 5, 7)
    block6 = _block(result, DAMAGED_BLOCK)
    assert block6.status == "CRC failed"
    assert block6.payload is None and block6.reconstruction is None
    assert all(b.status == "Intact" for b in result.blocks if b.block_no != DAMAGED_BLOCK)
    assert result.program_payload() is None
    assert not decision.permitted
    assert decision.blockers == ((DAMAGED_BLOCK, "crc_failed"),)
    with pytest.raises(ExportRefused):
        build_uef(result, decision)


# --- 14.3: full recovery ---------------------------------------------------------------

def test_full_recovery_reconstructs_block6_and_keeps_others_intact(full):
    result, decision = full
    assert [b.block_no for b in result.blocks] == list(range(BLOCK_COUNT))
    assert {b.block_no: b.status for b in result.blocks} == {
        n: (RECONSTRUCTED if n == DAMAGED_BLOCK else "Intact") for n in range(BLOCK_COUNT)
    }
    assert result.final_block == BLOCK_COUNT - 1
    assert decision.permitted, decision.blockers
    assert decision.reconstructed_blocks == (DAMAGED_BLOCK,)


def test_block6_choices_match_damage_record(full, decoded, damage, reference):
    result, _ = full
    block6 = _block(result, DAMAGED_BLOCK)
    rec = block6.reconstruction
    assert rec is not None and rec.reconstructed
    assert (rec.candidate_count, rec.evaluated_count, rec.match_count) == (16, 16, 1)
    assert block6.payload == bytes.fromhex(reference["blocks"][DAMAGED_BLOCK]["hex"])

    cid = {"a": decoded.a.capture_id, "b": decoded.b.capture_id}
    other = {"a": "b", "b": "a"}
    expected = {}
    for which in ("a", "b"):
        for d in damage[which]:
            # The damaged capture saw ``damaged``; the other capture saw the
            # original, which is chosen and attributed to the other capture.
            expected[d["offset"]] = (
                {cid[which]: d["damaged"], cid[other[which]]: d["original"]},
                d["original"],
                (cid[other[which]],),
            )
    got = {c.offset: (dict(c.observed), c.chosen, c.supporting_capture_ids) for c in rec.choices}
    assert got == expected
    assert sorted(got) == [17, 64, 143, 201]
    assert {o for o, (_, _, s) in got.items() if s == (cid["b"],)} == {17, 143}
    assert {o for o, (_, _, s) in got.items() if s == (cid["a"],)} == {64, 201}


def test_swapped_input_order_gives_identical_report_and_image(full, decoded):
    result, decision = full
    swapped = recover([decoded.b, decoded.a])
    swapped_decision = export_decision(swapped)
    assert dumps_report(build_report(swapped, swapped_decision)) == dumps_report(
        build_report(result, decision)
    )
    assert build_uef(swapped, swapped_decision) == build_uef(result, decision)


# --- 14.4: exported image ----------------------------------------------------------------

def test_exported_uef_matches_reference_and_recorded_crcs(
    full, decoded, reference, clean_uef, tmp_path
):
    result, decision = full
    out = tmp_path / "recovered.uef"
    export_uef(result, decision, out)
    blocks = read_uef(out)

    assert [fb.block_no for fb in blocks] == list(range(BLOCK_COUNT))
    program = b"".join(fb.payload for fb in blocks)
    assert program == b"".join(bytes.fromhex(r["hex"]) for r in reference["blocks"])
    assert len(program) == reference["program_length"]
    assert hashlib.sha256(program).hexdigest() == reference["program_sha256"]
    assert program == result.program_payload()

    for fb, ref in zip(blocks, reference["blocks"], strict=True):
        n = fb.block_no
        assert fb.header_crc_ok and fb.data_crc_ok, f"block {n}: CRC does not recompute"
        assert fb.payload.hex() == ref["hex"]
        assert fb.header_raw.hex() == ref["header_raw"]
        assert fb.header_crc_in_file == ref["header_crc"]
        assert fb.data_crc_in_file == ref["data_crc"]
        for cap in (decoded.a, decoded.b):
            obs = _observation(cap, n)
            assert fb.header_raw == obs.header_raw
            assert fb.header_crc_in_file == obs.header_crc.recorded
            assert obs.data_crc is not None
            assert fb.data_crc_in_file == obs.data_crc.recorded

    assert blocks[DAMAGED_BLOCK].data_crc_in_file == ORIGINAL_DATA_CRC
    assert out.read_bytes() == clean_uef


# --- CLI ------------------------------------------------------------------------------

def test_cli_full_recovery_both_orders(tmp_path, damage, reference, clean_uef):
    a, b = str(DAMAGED["a"]), str(DAMAGED["b"])
    runs = {}
    for label, order in (("ab", (a, b)), ("ba", (b, a))):
        report, uef = tmp_path / f"r_{label}.json", tmp_path / f"{label}.uef"
        proc = _cli("recover", *order, "--report", str(report), "--uef", str(uef),
                    "--decoder", str(DECODER), cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == ""
        assert f"block   {DAMAGED_BLOCK}  {RECONSTRUCTED}" in proc.stdout
        assert "export: permitted" in proc.stdout
        assert f"reconstructed blocks in this image: {DAMAGED_BLOCK} " in proc.stdout
        assert f"recovered payload sha256: {reference['program_sha256']}" in proc.stdout
        runs[label] = (report.read_bytes(), uef.read_bytes())

    assert runs["ab"] == runs["ba"]
    report_bytes, uef_bytes = runs["ab"]
    assert uef_bytes == clean_uef

    doc = json.loads(report_bytes)
    assert [blk["status"] for blk in doc["blocks"]] == [
        RECONSTRUCTED if n == DAMAGED_BLOCK else "Intact" for n in range(BLOCK_COUNT)
    ]
    assert doc["recovered_payload_sha256"] == reference["program_sha256"]
    assert sorted(i["sha256"] for i in doc["inputs"]) == sorted(damage["wav_sha256"].values())
    block6 = doc["blocks"][DAMAGED_BLOCK]
    assert block6["recorded_data_crc"] == ORIGINAL_DATA_CRC
    assert block6["reconstruction"]["candidate_count"] == 16
    assert block6["reconstruction"]["match_count"] == 1
    cid_a = damage["wav_sha256"]["damaged_a.wav"]
    cid_b = damage["wav_sha256"]["damaged_b.wav"]
    sources = {c["offset"]: (c["chosen"], c["supporting_capture_ids"])
               for c in block6["reconstruction"]["choices"]}
    assert sources == {
        **{d["offset"]: (d["original"], [cid_b]) for d in damage["a"]},
        **{d["offset"]: (d["original"], [cid_a]) for d in damage["b"]},
    }

    verify = _cli("verify-uef", str(tmp_path / "ab.uef"), "--reference", str(REFERENCE_JSON),
                  cwd=tmp_path)
    assert verify.returncode == 0, verify.stderr
    assert f"verified: {BLOCK_COUNT} blocks match the reference" in verify.stdout


def test_cli_clean_capture_exports_the_same_image(tmp_path, clean_uef):
    uef = tmp_path / "clean.uef"
    proc = _cli("recover", str(CLEAN_WAV), "--uef", str(uef), "--decoder", str(DECODER),
                cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "reconstructed blocks in this image: none" in proc.stdout
    assert uef.read_bytes() == clean_uef


def _unsupported_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 2205)
    return path


@pytest.mark.parametrize(
    "case, code, reason",
    [
        ("single_a", 4, "error: export refused: block 6: single_read"),
        ("single_b", 4, "error: export refused: block 6: single_read"),
        ("intact_only", 4, "error: export refused: block 6: crc_failed"),
        ("unsupported_wav", 3, "22050 Hz, 16-bit, 1 channel; TapeRescue supports 44100 Hz"),
        ("missing_capture", 3, "error: capture not found:"),
        ("missing_decoder", 6, "error: decoder not found at"),
        ("three_captures", 2, "error: recover accepts one or two captures (got 3)"),
        ("uef_is_input", 2, "error: the UEF output must not be the input capture"),
    ],
)
def test_cli_failure_paths_exit_nonzero_with_reason(tmp_path, case, code, reason):
    a, b = str(DAMAGED["a"]), str(DAMAGED["b"])
    uef, report = tmp_path / "out.uef", tmp_path / "report.json"
    dec = ["--decoder", str(DECODER)]
    args = {
        "single_a": ["recover", a, "--uef", str(uef), *dec],
        "single_b": ["recover", b, "--uef", str(uef), *dec],
        "intact_only": ["recover", a, b, "--intact-only", "--report", str(report),
                        "--uef", str(uef), *dec],
        "unsupported_wav": ["recover", str(_unsupported_wav(tmp_path / "slow.wav")), a,
                            "--uef", str(uef), *dec],
        "missing_capture": ["recover", a, str(tmp_path / "absent.wav"), "--uef", str(uef), *dec],
        "missing_decoder": ["recover", a, b, "--uef", str(uef),
                            "--decoder", str(tmp_path / "no-such-ScanTape")],
        "three_captures": ["recover", str(CLEAN_WAV), a, b, "--uef", str(uef), *dec],
        "uef_is_input": ["recover", a, b, "--uef", a, *dec],
    }[case]
    digests = {p: _sha256(p) for p in INPUTS}

    proc = _cli(*args, cwd=tmp_path)

    assert proc.returncode == code, (proc.returncode, proc.stdout, proc.stderr)
    assert reason in proc.stderr
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith("error: ") and len(last) > len("error: ")
    assert not uef.exists(), "no image may be written on a failure path"
    assert {p: _sha256(p) for p in INPUTS} == digests

    if case == "intact_only":
        # The report is still written when export is refused (Requirement 10.8).
        doc = json.loads(report.read_text(encoding="utf-8"))
        assert doc["reconstruction_enabled"] is False
        assert doc["blocks"][DAMAGED_BLOCK]["status"] == "CRC failed"
        assert doc["blocks"][DAMAGED_BLOCK]["reconstruction"]["attempted"] is False
        assert doc["export"]["permitted"] is False
        assert "recovered_payload_sha256" not in doc


# --- 10.7: inputs unchanged ----------------------------------------------------------

def test_input_digests_unchanged(decoded, damage, reference):
    after = {p: _sha256(p) for p in INPUTS}
    assert after == decoded.digests_before
    assert after[CLEAN_WAV] == reference["wav_sha256"]["clean.wav"]
    for which, path in DAMAGED.items():
        name = f"damaged_{which}.wav"
        assert after[path] == damage["wav_sha256"][name] == reference["wav_sha256"][name]
