"""Tests for the CLI commands.

``fixtures`` and ``decode``: task 7 (Requirements 16.1, 16.3).
``recover``, ``export`` and ``verify-uef``: task 9.5 (Requirements 11.2, 16.1,
16.2, 16.3).
Two-capture ``recover``/``export``, ``--report``, ``--intact-only``, input
digest checks and ``demo``: task 16.1 (Requirements 10.7, 10.8, 14.5, 16.1,
16.2, 16.3).
Creating missing parent directories of output paths: task 25.3
(Requirements 16.1, 16.3).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from taperescue import cli
from taperescue.decoder_adapter import capture_id_for
from taperescue.decoder_adapter.schema import observations_to_doc
from taperescue.fixtures.block_builder import TapeBlock, build_blocks
from taperescue.models import BlockObservation, CaptureObservations, CrcReading, HeaderFields

ROOT = Path(__file__).resolve().parents[1]
DECODER = ROOT / "build" / "decoder" / "ScanTape"
FIXTURES = ROOT / "fixtures" / "starmap"
CLEAN_WAV = FIXTURES / "clean.wav"
needs_decoder = pytest.mark.skipif(
    not (DECODER.is_file() and os.access(DECODER, os.X_OK)),
    reason=f"patched decoder not built at {DECODER}; run scripts/build_decoder.sh",
)


def _wav(path: Path, *, rate: int = 44_100, channels: int = 1, seconds: float = 0.1) -> Path:
    align = 2 * channels
    data = b"\0" * (int(rate * seconds) * align)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * align, align, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="ascii")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _assert_error(code: int, err: str, expected_code: int, fragment: str) -> None:
    assert code == expected_code
    lines = err.strip().splitlines()
    assert lines[-1].startswith("error: ")
    assert fragment in lines[-1]


# --- failure paths -----------------------------------------------------------

def test_missing_capture(capsys, tmp_path):
    code, out, err = _run(capsys, ["decode", str(tmp_path / "absent.wav")])
    _assert_error(code, err, cli.EXIT_CAPTURE_REJECTED, "capture not found")
    assert out == ""


def test_non_wav_capture(capsys, tmp_path):
    path = tmp_path / "notes.wav"
    path.write_text("not audio", encoding="ascii")
    code, _, err = _run(capsys, ["decode", str(path)])
    _assert_error(code, err, cli.EXIT_CAPTURE_REJECTED, "File is not a WAV file")


def test_unsupported_wav_format_reports_detected_values(capsys, tmp_path):
    path = _wav(tmp_path / "stereo.wav", rate=48_000, channels=2)
    code, _, err = _run(capsys, ["decode", str(path)])
    _assert_error(code, err, cli.EXIT_CAPTURE_REJECTED, "48000 Hz, 16-bit, 2 channels")


def test_capture_is_checked_before_the_decoder(capsys, tmp_path):
    code, _, err = _run(capsys, ["decode", str(tmp_path / "absent.wav"),
                                 "--decoder", str(tmp_path / "no-decoder")])
    _assert_error(code, err, cli.EXIT_CAPTURE_REJECTED, "capture not found")


def test_missing_decoder_option(capsys, tmp_path):
    wav = _wav(tmp_path / "a.wav")
    code, _, err = _run(capsys, ["decode", str(wav), "--decoder", str(tmp_path / "no-decoder")])
    _assert_error(code, err, cli.EXIT_DECODER_FAILED, "decoder not found")


def test_missing_decoder_from_environment(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv(cli.DECODER_ENV, str(tmp_path / "no-decoder"))
    wav = _wav(tmp_path / "a.wav")
    code, _, err = _run(capsys, ["decode", str(wav)])
    _assert_error(code, err, cli.EXIT_DECODER_FAILED, str(tmp_path / "no-decoder"))


def test_decoder_nonzero_exit(capsys, tmp_path):
    decoder = _script(tmp_path / "fail.sh", "echo boom >&2\nexit 3")
    wav = _wav(tmp_path / "a.wav")
    code, _, err = _run(capsys, ["decode", str(wav), "--decoder", str(decoder)])
    _assert_error(code, err, cli.EXIT_DECODER_FAILED, "Decoder failed on this capture")


def test_decoder_invalid_output_names_capture(capsys, tmp_path):
    # The adapter passes the output path as argv[4].
    decoder = _script(tmp_path / "bad.sh", 'printf "not json" > "$4"')
    wav = _wav(tmp_path / "a.wav")
    code, _, err = _run(capsys, ["decode", str(wav), "--decoder", str(decoder)])
    _assert_error(code, err, cli.EXIT_INVALID_OUTPUT, capture_id_for(wav))


def test_decoder_timeout(capsys, tmp_path):
    decoder = _script(tmp_path / "slow.sh", "exec sleep 30")
    wav = _wav(tmp_path / "a.wav")
    code, _, err = _run(capsys, ["decode", str(wav), "--decoder", str(decoder),
                                 "--timeout", "0.5"])
    _assert_error(code, err, cli.EXIT_DECODER_TIMEOUT, "timed out")


def test_output_must_not_overwrite_capture(capsys, tmp_path):
    wav = _wav(tmp_path / "a.wav")
    before = wav.read_bytes()
    code, _, err = _run(capsys, ["decode", str(wav), "-o", str(wav)])
    _assert_error(code, err, cli.EXIT_USAGE, "must not be the input capture")
    assert wav.read_bytes() == before


def test_fixtures_unwritable_output(capsys, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="ascii")
    code, _, err = _run(capsys, ["fixtures", "--out", str(blocker / "sub")])
    _assert_error(code, err, cli.EXIT_FAILURE, "cannot write fixtures")


def test_usage_errors(capsys):
    code, _, err = _run(capsys, [])
    _assert_error(code, err, cli.EXIT_USAGE, "no command given")
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == cli.EXIT_USAGE
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("error: ")


def test_installed_entry_point_reports_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "taperescue.cli", "decode", str(tmp_path / "absent.wav")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == cli.EXIT_CAPTURE_REJECTED
    assert proc.stderr.startswith("error: capture not found")


# --- success paths -----------------------------------------------------------

def test_fixtures_command_matches_checked_in_files(capsys, tmp_path):
    code, out, _ = _run(capsys, ["fixtures", "--out", str(tmp_path)])
    assert code == 0
    for name in ("clean.wav", "damaged_a.wav", "damaged_b.wav", "reference_payloads.json",
                 "damage.json", "STARMAP.bas"):
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), name
    assert capture_id_for(CLEAN_WAV) in out


@needs_decoder
def test_decode_clean_fixture(capsys, tmp_path):
    out_path = tmp_path / "obs.json"
    code, _, _ = _run(capsys, ["decode", str(CLEAN_WAV), "--decoder", str(DECODER),
                               "-o", str(out_path)])
    assert code == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    reference = json.loads((FIXTURES / "reference_payloads.json").read_text(encoding="ascii"))
    assert doc["capture_id"] == capture_id_for(CLEAN_WAV)
    assert [b["header"]["block_no"] for b in doc["blocks"]] == list(range(8))
    for block, ref in zip(doc["blocks"], reference["blocks"]):
        assert block["header_crc"] == {"outcome": "valid", "recorded": ref["header_crc"]}
        assert block["data_crc"] == {"outcome": "valid", "recorded": ref["data_crc"]}
        assert block["payload"] == ref["hex"]
    assert doc["blocks"][7]["header"]["flags"] & 0x80


@needs_decoder
def test_decode_to_stdout_damaged_block_six(capsys):
    code, out, _ = _run(capsys, ["decode", str(FIXTURES / "damaged_a.wav"),
                                 "--decoder", str(DECODER)])
    assert code == 0
    damage = json.loads((FIXTURES / "damage.json").read_text(encoding="ascii"))
    block6 = json.loads(out)["blocks"][6]
    assert block6["data_crc"] == {"outcome": "invalid", "recorded": damage["data_crc"]}


@needs_decoder
def test_decode_silent_capture_has_no_blocks(capsys, tmp_path):
    wav = _wav(tmp_path / "silent.wav", seconds=1.0)
    code, _, err = _run(capsys, ["decode", str(wav), "--decoder", str(DECODER)])
    _assert_error(code, err, cli.EXIT_NO_BLOCKS, "No BBC 1200-baud blocks")


# --- recover, export, verify-uef (task 9.5) ----------------------------------

REFERENCE = FIXTURES / "reference_payloads.json"
SYNTH_CID = "c" * 64
SYNTH_PROGRAM = random.Random(95).randbytes(600)  # 3 blocks: 256 + 256 + 88


def _synthetic_obs(block: TapeBlock, payload: bytes | None = None,
                   cid: str = SYNTH_CID) -> BlockObservation:
    payload = block.payload if payload is None else payload
    outcome = "valid" if payload == block.payload else "invalid"
    return BlockObservation(
        capture_id=cid,
        sample_offset=10_000 * (block.block_no + 1),
        header_raw=block.header_raw,
        header=HeaderFields(block.filename, block.load_addr, block.exec_addr, block.block_no,
                            len(block.payload), block.flags, block.next_addr),
        header_crc=CrcReading("valid", block.header_crc),
        payload=payload,
        expected_len=len(block.payload),
        actual_len=len(payload),
        data_crc=CrcReading(outcome, block.data_crc),
    )


def _observations_file(path: Path, *, damage_block: int | None = None, damage_offset: int = 0,
                       cid: str = SYNTH_CID, program: bytes = SYNTH_PROGRAM,
                       filename: str = "SYNTH") -> Path:
    """Write an observation document as ``decode`` would, from built blocks.

    The damaged block keeps its original recorded data CRC, with bit 0 of the
    byte at ``damage_offset`` flipped.
    """
    obs = []
    for block in build_blocks(program, filename=filename):
        payload = None
        if block.block_no == damage_block:
            damaged = bytearray(block.payload)
            damaged[damage_offset] ^= 0x01
            payload = bytes(damaged)
        obs.append(_synthetic_obs(block, payload, cid))
    doc = observations_to_doc(CaptureObservations(cid, tuple(obs)))
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _reference_file(path: Path, program: bytes) -> Path:
    blocks = build_blocks(program, filename="SYNTH")
    ref = {
        "program_sha256": hashlib.sha256(program).hexdigest(),
        "blocks": [{"block_no": b.block_no, "hex": b.payload.hex(),
                    "header_raw": b.header_raw.hex(), "header_crc": b.header_crc,
                    "data_crc": b.data_crc} for b in blocks],
    }
    path.write_text(json.dumps(ref), encoding="utf-8")
    return path


def test_recover_rejects_three_captures(capsys, tmp_path):
    wavs = [str(_wav(tmp_path / f"{n}.wav")) for n in "abc"]
    code, out, err = _run(capsys, ["recover", *wavs])
    _assert_error(code, err, cli.EXIT_USAGE, "accepts one or two captures (got 3)")
    assert out == ""


def test_recover_missing_capture(capsys, tmp_path):
    code, _, err = _run(capsys, ["recover", str(tmp_path / "absent.wav")])
    _assert_error(code, err, cli.EXIT_CAPTURE_REJECTED, "capture not found")


def test_recover_uef_must_not_be_the_capture(capsys, tmp_path):
    wav = _wav(tmp_path / "a.wav")
    before = wav.read_bytes()
    code, _, err = _run(capsys, ["recover", str(wav), "--uef", str(wav)])
    _assert_error(code, err, cli.EXIT_USAGE, "must not be the input capture")
    assert wav.read_bytes() == before


def test_export_from_observations_round_trips_through_verify(capsys, tmp_path):
    obs = _observations_file(tmp_path / "obs.json")
    out = tmp_path / "out.uef"
    code, stdout, _ = _run(capsys, ["export", "--observations", str(obs), "-o", str(out)])
    assert code == 0
    assert stdout.count("Intact") == 3 and "export: permitted" in stdout
    ref = _reference_file(tmp_path / "ref.json", SYNTH_PROGRAM)
    code, stdout, _ = _run(capsys, ["verify-uef", str(out), "--reference", str(ref)])
    assert code == 0
    assert hashlib.sha256(SYNTH_PROGRAM).hexdigest() in stdout
    assert "verified: 3 blocks match the reference" in stdout


def test_export_refused_prints_blockers_and_writes_nothing(capsys, tmp_path):
    obs = _observations_file(tmp_path / "obs.json", damage_block=1)
    out = tmp_path / "out.uef"
    code, stdout, err = _run(capsys, ["export", "--observations", str(obs), "-o", str(out)])
    _assert_error(code, err, cli.EXIT_EXPORT_REFUSED, "block 1: single_read")
    assert "block   1  CRC failed  (single_read)" in stdout
    assert "blocker: block 1: single_read" in stdout
    assert not out.exists()
    assert list(tmp_path.iterdir()) == [obs]


def test_export_rejects_three_observations_files(capsys, tmp_path):
    obs = _observations_file(tmp_path / "obs.json")
    code, _, err = _run(capsys, ["export", "--observations", str(obs), str(obs), str(obs),
                                 "-o", str(tmp_path / "out.uef")])
    _assert_error(code, err, cli.EXIT_USAGE, "accepts one or two captures (got 3)")


@pytest.mark.parametrize("text, fragment", [
    ("not json", "not a JSON document"),
    (json.dumps({"schema": "taperescue.observations/1", "capture_id": "xyz", "blocks": []}),
     "capture_id must be 64 lowercase hex digits"),
    (json.dumps({"schema": "other", "capture_id": SYNTH_CID, "blocks": []}),
     "Decoder output was invalid"),
])
def test_export_invalid_observations(capsys, tmp_path, text, fragment):
    obs = tmp_path / "obs.json"
    obs.write_text(text, encoding="utf-8")
    code, _, err = _run(capsys, ["export", "--observations", str(obs),
                                 "-o", str(tmp_path / "out.uef")])
    _assert_error(code, err, cli.EXIT_INVALID_OUTPUT, fragment)
    assert not (tmp_path / "out.uef").exists()


def test_verify_uef_reports_mismatching_block(capsys, tmp_path):
    obs = _observations_file(tmp_path / "obs.json")
    out = tmp_path / "out.uef"
    assert cli.main(["export", "--observations", str(obs), "-o", str(out)]) == 0
    capsys.readouterr()
    other = bytearray(SYNTH_PROGRAM)
    other[300] ^= 0x40  # inside block 1
    ref = _reference_file(tmp_path / "ref.json", bytes(other))
    code, stdout, err = _run(capsys, ["verify-uef", str(out), "--reference", str(ref)])
    _assert_error(code, err, cli.EXIT_VERIFY_FAILED, "does not match the reference")
    lines = stdout.splitlines()
    assert lines[0].endswith(" ok") and lines[2].endswith(" ok")
    assert "MISMATCH: payload differs from the reference" in lines[1]


def test_verify_uef_rejects_non_uef(capsys, tmp_path):
    bad = tmp_path / "bad.uef"
    bad.write_bytes(b"not a tape image")
    code, _, err = _run(capsys, ["verify-uef", str(bad), "--reference", str(REFERENCE)])
    _assert_error(code, err, cli.EXIT_VERIFY_FAILED, "UEF File!")


def test_verify_uef_missing_reference(capsys, tmp_path):
    code, _, err = _run(capsys, ["verify-uef", str(tmp_path / "x.uef"),
                                 "--reference", str(tmp_path / "absent.json")])
    _assert_error(code, err, cli.EXIT_FAILURE, "reference not found")


@needs_decoder
def test_recover_clean_fixture_exports_reference_program(capsys, tmp_path):
    out = tmp_path / "clean.uef"
    code, stdout, _ = _run(capsys, ["recover", str(CLEAN_WAV), "--uef", str(out),
                                    "--decoder", str(DECODER)])
    assert code == 0
    for n in range(8):
        assert f"block {n:>3}  Intact\n" in stdout
    assert "export: permitted" in stdout

    code, stdout, _ = _run(capsys, ["verify-uef", str(out), "--reference", str(REFERENCE)])
    assert code == 0
    reference = json.loads(REFERENCE.read_text(encoding="ascii"))
    assert f"payload sha256:   {reference['program_sha256']}" in stdout
    assert stdout.count(" ok\n") == 8

    # decode -> export --observations gives the same image bytes.
    obs = tmp_path / "clean.obs.json"
    assert cli.main(["decode", str(CLEAN_WAV), "--decoder", str(DECODER), "-o", str(obs)]) == 0
    from_obs = tmp_path / "from_obs.uef"
    assert cli.main(["export", "--observations", str(obs), "-o", str(from_obs)]) == 0
    assert from_obs.read_bytes() == out.read_bytes()


@needs_decoder
@pytest.mark.parametrize("name", ["damaged_a.wav", "damaged_b.wav"])
def test_recover_damaged_fixture_refuses_export(capsys, tmp_path, name):
    out = tmp_path / "out.uef"
    code, stdout, err = _run(capsys, ["recover", str(FIXTURES / name), "--uef", str(out),
                                      "--decoder", str(DECODER)])
    _assert_error(code, err, cli.EXIT_EXPORT_REFUSED, "block 6: single_read")
    assert "block   6  CRC failed  (single_read)" in stdout
    assert stdout.count("  Intact\n") == 7
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


@needs_decoder
def test_recover_without_uef_reports_single_capture(capsys):
    code, stdout, _ = _run(capsys, ["recover", str(FIXTURES / "damaged_a.wav"),
                                    "--decoder", str(DECODER)])
    assert code == 0
    assert "export: refused" in stdout and "blocker: block 6: single_read" in stdout


# --- two-capture recover, report, input integrity, demo (task 16.1) ----------
#
# Requirements 10.7, 10.8, 14.5, 16.1, 16.2, 16.3.

OTHER_CID = "d" * 64
DAMAGE_JSON = FIXTURES / "damage.json"
DAMAGED = [FIXTURES / "damaged_a.wav", FIXTURES / "damaged_b.wav"]


def _digests(paths) -> dict[Path, str]:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _pair_files(tmp_path: Path) -> list[Path]:
    """Two synthetic captures of SYNTH_PROGRAM, each failing block 1 at a different byte."""
    return [
        _observations_file(tmp_path / "a.json", damage_block=1, damage_offset=3),
        _observations_file(tmp_path / "b.json", damage_block=1, damage_offset=200, cid=OTHER_CID),
    ]


def test_export_two_observation_files_reconstructs_and_lists_blocks(capsys, tmp_path):
    a, b = _pair_files(tmp_path)
    out = tmp_path / "out.uef"
    code, stdout, _ = _run(capsys, ["export", "--observations", str(a), str(b), "-o", str(out)])
    assert code == 0
    assert "block   1  Reconstructed · CRC match" in stdout
    assert "candidates 4, evaluated 4, matches 1" in stdout
    assert "offset   3: a.json=" in stdout and "from b.json" in stdout
    assert "offset 200: a.json=" in stdout and "from a.json" in stdout
    # The reconstructed block list comes before the image is written.
    assert stdout.index("reconstructed blocks in this image: 1") < stdout.index(f"wrote {out}")
    ref = _reference_file(tmp_path / "ref.json", SYNTH_PROGRAM)
    assert cli.main(["verify-uef", str(out), "--reference", str(ref)]) == 0


def test_export_pair_without_anchor_is_rejected(capsys, tmp_path):
    a = _observations_file(tmp_path / "a.json")
    b = _observations_file(tmp_path / "b.json", cid=OTHER_CID, filename="OTHER",
                           program=random.Random(7).randbytes(400))
    out = tmp_path / "out.uef"
    code, stdout, err = _run(capsys, ["export", "--observations", str(a), str(b), "-o", str(out)])
    _assert_error(code, err, cli.EXIT_NO_ANCHOR,
                  "These recordings cannot be shown to belong to the same program. "
                  "Select recordings of the same program version.")
    assert stdout == ""
    assert not out.exists()


def test_inputs_unchanged_detects_a_modified_input(tmp_path):
    path = tmp_path / "capture.wav"
    path.write_bytes(b"original")
    with cli._inputs_unchanged([path]):
        pass
    with pytest.raises(cli.CliError) as exc:
        with cli._inputs_unchanged([path]):
            path.write_bytes(b"modified")
    assert exc.value.code == cli.EXIT_INPUT_CHANGED
    assert str(path) in exc.value.message


def test_inputs_unchanged_check_runs_when_the_body_fails(tmp_path):
    path = tmp_path / "capture.wav"
    path.write_bytes(b"original")
    with pytest.raises(cli.CliError) as exc:
        with cli._inputs_unchanged([path]):
            path.unlink()
            raise cli.CliError("decoder failed", cli.EXIT_DECODER_FAILED)
    assert exc.value.code == cli.EXIT_INPUT_CHANGED


@pytest.mark.parametrize("extra, fragment", [
    (["--report", "{a}"], "the report must not be the input capture"),
    (["--report", "{out}", "--uef", "{out}"], "must be different files"),
])
def test_recover_output_collisions(capsys, tmp_path, extra, fragment):
    a = _wav(tmp_path / "a.wav")
    before = a.read_bytes()
    args = [x.format(a=a, out=tmp_path / "out") for x in extra]
    code, _, err = _run(capsys, ["recover", str(a), *args])
    _assert_error(code, err, cli.EXIT_USAGE, fragment)
    assert a.read_bytes() == before


def test_demo_missing_fixtures(capsys, tmp_path):
    code, _, err = _run(capsys, ["demo", "--fixtures", str(tmp_path), "--out", str(tmp_path / "o")])
    _assert_error(code, err, cli.EXIT_CAPTURE_REJECTED, "capture not found")
    assert not (tmp_path / "o").exists()


@needs_decoder
def test_recover_two_damaged_fixtures_in_either_order(capsys, tmp_path):
    before = _digests(DAMAGED)
    damage = json.loads(DAMAGE_JSON.read_text(encoding="ascii"))
    runs = []
    for tag, order in (("ab", DAMAGED), ("ba", DAMAGED[::-1])):
        report, uef = tmp_path / f"{tag}.json", tmp_path / f"{tag}.uef"
        code, stdout, _ = _run(capsys, ["recover", *map(str, order), "--report", str(report),
                                        "--uef", str(uef), "--decoder", str(DECODER)])
        assert code == 0
        assert "block   6  Reconstructed · CRC match" in stdout
        assert stdout.count("  Intact\n") == 7
        assert "reconstructed blocks in this image: 6" in stdout
        for flip in damage["a"]:  # damaged in A, so B supplies the original byte
            assert (f"offset {flip['offset']:>3}: damaged_a.wav=0x{flip['damaged']:02X} "
                    f"damaged_b.wav=0x{flip['original']:02X} -> chosen "
                    f"0x{flip['original']:02X} from damaged_b.wav") in stdout
        for flip in damage["b"]:
            assert f"0x{flip['original']:02X} from damaged_a.wav" in stdout
        runs.append((report.read_bytes(), uef.read_bytes()))
    assert runs[0] == runs[1]
    assert _digests(DAMAGED) == before

    doc = json.loads(runs[0][0])
    reference = json.loads(REFERENCE.read_text(encoding="ascii"))
    assert doc["recovered_payload_sha256"] == reference["program_sha256"]
    assert doc["export"]["reconstructed_blocks"] == [6]
    code, stdout, _ = _run(capsys, ["verify-uef", str(tmp_path / "ab.uef"),
                                    "--reference", str(REFERENCE)])
    assert code == 0 and "verified: 8 blocks match the reference" in stdout


@needs_decoder
def test_recover_intact_only_writes_report_and_refuses_export(capsys, tmp_path):
    report, uef = tmp_path / "r.json", tmp_path / "out.uef"
    code, stdout, err = _run(capsys, ["recover", *map(str, DAMAGED), "--intact-only",
                                      "--report", str(report), "--uef", str(uef),
                                      "--decoder", str(DECODER)])
    _assert_error(code, err, cli.EXIT_EXPORT_REFUSED, "block 6: crc_failed")
    assert "block   6  CRC failed\n" in stdout
    assert not uef.exists()
    doc = json.loads(report.read_text(encoding="utf-8"))
    block6 = doc["blocks"][6]
    assert doc["reconstruction_enabled"] is False
    assert block6["status"] == "CRC failed" and block6["payload_sha256"] is None
    assert block6["reconstruction"]["attempted"] is False
    assert doc["export"]["permitted"] is False
    assert "recovered_payload_sha256" not in doc


@needs_decoder
def test_recover_single_capture_writes_report(capsys, tmp_path):
    report = tmp_path / "r.json"
    code, stdout, _ = _run(capsys, ["recover", str(DAMAGED[0]), "--report", str(report),
                                    "--decoder", str(DECODER)])
    assert code == 0 and "export: refused" in stdout
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert [i["sha256"] for i in doc["inputs"]] == [capture_id_for(DAMAGED[0])]
    assert doc["blocks"][6]["status"] == "CRC failed"
    assert doc["blocks"][6]["reason"] == "single_read"


@needs_decoder
def test_recover_unrelated_programs_have_no_anchor(capsys, tmp_path):
    from taperescue.fixtures.block_builder import block_frames
    from taperescue.fixtures.fsk import modulate, tape_stream

    blocks = build_blocks(random.Random(161).randbytes(600), filename="OTHER")
    other = tmp_path / "other.wav"
    other.write_bytes(modulate(tape_stream([block_frames(b) for b in blocks])))
    report, uef = tmp_path / "r.json", tmp_path / "out.uef"
    code, stdout, err = _run(capsys, ["recover", str(CLEAN_WAV), str(other), "--report",
                                      str(report), "--uef", str(uef), "--decoder", str(DECODER)])
    _assert_error(code, err, cli.EXIT_NO_ANCHOR, "cannot be shown to belong to the same program")
    assert stdout == ""
    assert not report.exists() and not uef.exists()


@needs_decoder
def test_demo_end_to_end(capsys, tmp_path):
    before = _digests([*DAMAGED, REFERENCE])
    out = tmp_path / "demo"
    code, stdout, _ = _run(capsys, ["demo", "--out", str(out), "--decoder", str(DECODER)])
    assert code == 0
    assert "damaged_a.wav: block 6 CRC failed  (single_read)" in stdout
    assert "damaged_b.wav: block 6 CRC failed  (single_read)" in stdout
    assert "block 6: CRC failed (not recovered); export refused (block 6: crc_failed)" in stdout
    assert "block   6  Reconstructed · CRC match" in stdout
    reference = json.loads(REFERENCE.read_text(encoding="ascii"))
    assert f"payload sha256:   {reference['program_sha256']}" in stdout
    assert f"reference sha256: {reference['program_sha256']}" in stdout
    assert "verified: 8 blocks match the reference" in stdout
    doc = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert doc["recovered_payload_sha256"] == reference["program_sha256"]
    assert (out / "STARMAP.uef").is_file()
    assert _digests([*DAMAGED, REFERENCE]) == before


# --- missing parent directories of output paths (task 25.3) ------------------
#
# Requirements 16.1, 16.3.

def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_export_creates_missing_output_directories(capsys, tmp_path):
    obs = _observations_file(tmp_path / "obs.json")
    out = tmp_path / "x" / "y" / "out.uef"
    code, stdout, _ = _run(capsys, ["export", "--observations", str(obs), "-o", str(out)])
    assert code == 0 and f"wrote {out}" in stdout
    # Only the image is left in the new directory: no exporter temporary file.
    assert list(out.parent.iterdir()) == [out]
    ref = _reference_file(tmp_path / "ref.json", SYNTH_PROGRAM)
    assert cli.main(["verify-uef", str(out), "--reference", str(ref)]) == 0


def test_refused_export_creates_no_directory(capsys, tmp_path):
    obs = _observations_file(tmp_path / "obs.json", damage_block=1)
    out = tmp_path / "new" / "out.uef"
    code, _, err = _run(capsys, ["export", "--observations", str(obs), "-o", str(out)])
    _assert_error(code, err, cli.EXIT_EXPORT_REFUSED, "block 1: single_read")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("parts, fragment", [
    (("file", "out.uef"), "it exists and is not a directory"),
    (("file", "sub", "out.uef"), "Not a directory"),
])
def test_output_parent_is_a_regular_file(capsys, tmp_path, parts, fragment):
    obs = _observations_file(tmp_path / "obs.json")
    (tmp_path / "file").write_text("x", encoding="ascii")
    out = tmp_path.joinpath(*parts)
    code, _, err = _run(capsys, ["export", "--observations", str(obs), "-o", str(out)])
    _assert_error(code, err, cli.EXIT_FAILURE, "cannot create directory")
    assert fragment in err
    assert (tmp_path / "file").read_text(encoding="ascii") == "x"


def test_output_equal_to_input_creates_no_directory(capsys, tmp_path):
    wav = _wav(tmp_path / "a.wav")
    before = _tree(tmp_path)
    code, _, err = _run(capsys, ["recover", str(wav), "--report",
                                 str(tmp_path / "new" / "r.json"), "--uef", str(wav)])
    _assert_error(code, err, cli.EXIT_USAGE, "must not be the input capture")
    assert _tree(tmp_path) == before


def test_fixtures_creates_nested_out(capsys, tmp_path):
    out = tmp_path / "x" / "y"
    code, _, _ = _run(capsys, ["fixtures", "--out", str(out)])
    assert code == 0
    assert (out / "clean.wav").read_bytes() == CLEAN_WAV.read_bytes()


@needs_decoder
def test_decode_creates_missing_output_directories(capsys, tmp_path):
    out = tmp_path / "x" / "y" / "obs.json"
    code, _, _ = _run(capsys, ["decode", str(CLEAN_WAV), "--decoder", str(DECODER),
                               "-o", str(out)])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["capture_id"] == capture_id_for(CLEAN_WAV)


@needs_decoder
def test_decode_output_parent_is_a_regular_file(capsys, tmp_path):
    (tmp_path / "file").write_text("x", encoding="ascii")
    code, _, err = _run(capsys, ["decode", str(CLEAN_WAV), "--decoder", str(DECODER),
                                 "-o", str(tmp_path / "file" / "obs.json")])
    _assert_error(code, err, cli.EXIT_FAILURE, "it exists and is not a directory")


@needs_decoder
def test_recover_creates_report_and_uef_directories(capsys, tmp_path):
    report = tmp_path / "r" / "s" / "report.json"
    uef = tmp_path / "u" / "v" / "STARMAP.uef"
    code, _, _ = _run(capsys, ["recover", *map(str, DAMAGED), "--report", str(report),
                               "--uef", str(uef), "--decoder", str(DECODER)])
    assert code == 0
    assert json.loads(report.read_text(encoding="utf-8"))["export"]["permitted"] is True
    assert list(uef.parent.iterdir()) == [uef]
    assert cli.main(["verify-uef", str(uef), "--reference", str(REFERENCE)]) == 0


@needs_decoder
def test_refused_recover_writes_report_in_new_directory_but_no_uef(capsys, tmp_path):
    report = tmp_path / "r" / "s" / "report.json"
    uef = tmp_path / "u" / "v" / "STARMAP.uef"
    code, _, err = _run(capsys, ["recover", str(DAMAGED[0]), "--report", str(report),
                                 "--uef", str(uef), "--decoder", str(DECODER)])
    _assert_error(code, err, cli.EXIT_EXPORT_REFUSED, "block 6: single_read")
    assert json.loads(report.read_text(encoding="utf-8"))["export"]["permitted"] is False
    assert not (tmp_path / "u").exists()


@needs_decoder
def test_demo_creates_nested_out(capsys, tmp_path):
    out = tmp_path / "x" / "y" / "demo"
    code, _, _ = _run(capsys, ["demo", "--out", str(out), "--decoder", str(DECODER)])
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["STARMAP.uef", "report.json"]


@needs_decoder
def test_benchmark_creates_nested_out(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("QUADBIKE_LSBLOCKS", raising=False)
    monkeypatch.delenv("QUADBIKE_BIN", raising=False)
    out = tmp_path / "x" / "y"
    code, _, _ = _run(capsys, ["benchmark", "--out", str(out), "--decoder", str(DECODER)])
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["results.json", "results.md"]
