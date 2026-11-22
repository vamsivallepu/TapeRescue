"""Tests for the STARMAP program layout and the fixture generator (task 6.8)."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from taperescue.crc import crc16_xmodem
from taperescue.fixtures import starmap
from taperescue.fixtures.starmap import (
    DamagePlan,
    StarmapLayoutError,
    build_program,
    check_plan,
    generate,
    program_lines,
    select_damage,
)
from taperescue.fixtures.tokenizer import detokenize, normalise
from taperescue.wavcheck import inspect_wav_header

SOURCE = starmap.DEFAULT_SOURCE.read_text(encoding="ascii")


@pytest.fixture(scope="module")
def program():
    return build_program(SOURCE)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("starmap"))


def test_source_is_starmap_and_round_trips(program):
    assert "MODE 1" in SOURCE and "RND(-42)" in SOURCE and "DATA" in SOURCE
    assert program.padding == ()
    assert detokenize(program.program) == normalise(SOURCE)


def test_eight_blocks_named_starmap_with_final_flag_on_block_7(program):
    blocks = program.blocks
    assert [b.block_no for b in blocks] == list(range(8))
    assert all(b.filename == "STARMAP" for b in blocks)
    assert [b.block_len for b in blocks[:7]] == [256] * 7
    assert 1 <= blocks[7].block_len <= 256
    assert [b.flags & 0x80 for b in blocks] == [0] * 7 + [0x80]
    assert b"".join(b.payload for b in blocks) == program.program
    assert all(crc16_xmodem(b.payload) == b.data_crc for b in blocks)


def test_proc_constellations_lies_inside_block_6_and_is_called_before_end(program):
    lines = program_lines(program.program)
    def_line = next(line for line in lines if line.text == "DEF PROCconstellations")
    end_proc = next(line for line in lines
                    if line.start > def_line.start and line.text == "ENDPROC")
    assert 1536 <= def_line.start and end_proc.end - 1 <= 1791
    assert (program.proc_start, program.proc_end) == (def_line.start, end_proc.end)
    end_line = next(line for line in lines if line.text == "END")
    assert any(line.text == "PROCconstellations" for line in lines if line.start < end_line.start)
    assert end_line.start < def_line.start


def test_short_source_is_padded_with_rem_lines_before_the_procedure():
    # Dropping the meteor procedure and its call moves PROCconstellations below 1536.
    removed = {"130", "1600", "1610", "1620", "1630", "1640"}
    short = "\n".join(line for line in SOURCE.splitlines() if line.split(" ")[0] not in removed)
    result = build_program(short + "\n")
    assert result.padding and all(p["where"] == "before_proc" for p in result.padding)
    assert 1536 <= result.proc_start and result.proc_end - 1 <= 1791
    assert len(result.blocks) == 8
    padded_text = detokenize(result.program)
    assert all(f"{p['line']} REM" in padded_text for p in result.padding)


def test_source_too_long_is_rejected():
    long = SOURCE.replace("1000 REM Stars: dim, medium and bright",
                          "1000 REM " + "x" * 240)
    with pytest.raises(StarmapLayoutError):
        build_program(long)


def test_planned_damage_is_usable_and_overlap_is_rejected(program):
    block6 = program.blocks[6]
    plan, rejected = select_damage(block6.payload, block6.data_crc)
    assert (plan.a, plan.b, plan.source) == (starmap.PLANNED_A, starmap.PLANNED_B, "planned")
    assert rejected == []
    overlap = DamagePlan(((17, 2), (143, 5)), ((17, 3), (201, 6)), "test", None)
    assert "A and B offsets overlap" in check_plan(block6.payload, block6.data_crc, overlap)


def test_fallback_list_is_fixed():
    assert starmap.fallback_plans(256) == starmap.fallback_plans(256)
    for plan in starmap.fallback_plans(256):
        offsets = [o for o, _ in plan.a + plan.b]
        assert len(set(offsets)) == 4


def test_generate_writes_consistent_records(generated):
    out = generated.out_dir
    ref = json.loads((out / "reference_payloads.json").read_text())
    dmg = json.loads((out / "damage.json").read_text())
    program = b"".join(bytes.fromhex(b["hex"]) for b in ref["blocks"])
    assert hashlib.sha256(program).hexdigest() == ref["program_sha256"]
    assert ref["filename"] == "STARMAP" and len(ref["blocks"]) == 8
    payload6 = bytes.fromhex(ref["blocks"][6]["hex"])
    assert dmg["data_crc"] == ref["blocks"][6]["data_crc"] == crc16_xmodem(payload6)
    assert {f["offset"] for f in dmg["a"]}.isdisjoint(f["offset"] for f in dmg["b"])
    for flip in dmg["a"] + dmg["b"]:
        assert flip["original"] == payload6[flip["offset"]]
        assert flip["damaged"] == flip["original"] ^ (1 << flip["bit"])
    assert dmg["candidate_check"] == {"candidate_count": 16, "match_count": 1}
    for name in ("clean.wav", "damaged_a.wav", "damaged_b.wav"):
        data = (out / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == ref["wav_sha256"][name]
        info = inspect_wav_header(data[:64], len(data))
        assert (info.sample_rate, info.bits_per_sample, info.channels) == (44100, 16, 1)
    assert (out / "STARMAP.bas").read_text() == SOURCE


def test_generate_is_deterministic(generated, tmp_path):
    again = generate(tmp_path)
    assert again.wav_sha256 == generated.wav_sha256
    for name in ("reference_payloads.json", "damage.json"):
        assert (tmp_path / name).read_bytes() == (generated.out_dir / name).read_bytes()


def test_checked_in_fixtures_match_the_generator(generated):
    repo = starmap.DEFAULT_SOURCE.parent
    for name in ("clean.wav", "damaged_a.wav", "damaged_b.wav",
                 "reference_payloads.json", "damage.json"):
        assert (repo / name).read_bytes() == (generated.out_dir / name).read_bytes(), name


def test_generator_does_not_import_the_decoder():
    tree = ast.parse(Path(starmap.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith("taperescue.decoder_adapter") for name in imported)
    assert "subprocess" not in imported
    assert "ScanTape" not in Path(starmap.__file__).read_text()
