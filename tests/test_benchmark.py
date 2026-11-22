"""Minimal checks for the baseline benchmark (task 17.1).

Fuller unit tests belong to task 17.2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taperescue import cli
from taperescue.benchmark import HEADING, parse_lsblocks, quadbike_comparison

DECODER = cli.DEFAULT_DECODER
FIXTURES = cli.DEFAULT_DEMO_FIXTURES

needs_decoder = pytest.mark.skipif(
    not DECODER.is_file() or not (FIXTURES / "damaged_a.wav").is_file(),
    reason="needs build/decoder/ScanTape and fixtures/starmap (run scripts/build_decoder.sh)",
)


def test_quadbike_absent_records_reason_and_runs_nothing():
    result = quadbike_comparison([("a.wav", Path("a.wav"))], env={}, which=lambda _: None)
    assert result["ran"] is False
    assert "php is not on PATH" in result["reason"]
    assert "QUADBIKE_LSBLOCKS is not set" in result["reason"]
    assert "runs" not in result


def test_parse_lsblocks_counts_rows_without_error_codes():
    stdout = "\n".join([
        "Decoded 6672 bytes.",
        '#  0 [  410382]     "FOOTER"      900      900    0  100 ffffffff',
        '#  1 [  557328]     "FOOTER"      900      900    1  100 ffffffff      DC',
        '#  2 [  704235]     "FOOTER"      900      900    2   30 ffffffff F',
        "M: Metrics|Errors|1|Blocks|3",
    ])
    parsed = parse_lsblocks(stdout)
    assert parsed["block_rows"] == 3
    assert parsed["rows_without_errors"] == 2
    assert parsed["metrics"] == {"errors": 1, "blocks": 3}


@needs_decoder
def test_benchmark_command_on_damaged_fixtures(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("QUADBIKE_LSBLOCKS", raising=False)
    monkeypatch.delenv("QUADBIKE_BIN", raising=False)
    assert cli.main(["benchmark", "--out", str(tmp_path / "a")]) == 0
    assert cli.main(["benchmark", "--out", str(tmp_path / "b")]) == 0
    for name in ("results.json", "results.md"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()

    md = (tmp_path / "a" / "results.md").read_text(encoding="utf-8")
    assert md.startswith(f"# {HEADING}\n")
    doc = json.loads((tmp_path / "a" / "results.json").read_text(encoding="utf-8"))
    assert doc["heading"] == HEADING
    modes = {m["mode"]: m for m in doc["modes"]}
    assert (modes["best_single_capture"]["recovered_blocks"],
            modes["best_single_capture"]["complete_program"]) == (7, False)
    assert (modes["intact_only_merge"]["recovered_blocks"],
            modes["intact_only_merge"]["complete_program"]) == (7, False)
    full = modes["taperescue"]
    assert (full["recovered_blocks"], full["complete_program"], full["matches_reference"]) == (
        8, True, True)
    assert full["reconstructed_blocks"] == [6]
    if doc["quadbike"]["ran"] is False and "runs" not in doc["quadbike"]:
        assert doc["quadbike"]["reason"]
