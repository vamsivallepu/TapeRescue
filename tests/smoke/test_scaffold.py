"""Smoke tests for the repository scaffold and verification log tooling (task 1)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_collector():
    spec = importlib.util.spec_from_file_location(
        "collect_verification", ROOT / "scripts" / "collect_verification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


def test_license_is_mit():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Permission is hereby granted, free of charge" in text


def test_package_modules_import():
    import taperescue
    import taperescue.decoder_adapter
    import taperescue.engine
    import taperescue.fixtures
    import taperescue.uef

    assert taperescue.__version__


def test_pending_items_cover_every_external_step():
    text = (ROOT / "verification" / "pending.md").read_text(encoding="utf-8")
    items = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    titles = [item.splitlines()[0] for item in items]
    expected = ["clean `.uef` in JSBeeb", "recovered `.uef` in JSBeeb", "real archival captures",
                "Quadbike", "functional URL", "YouTube demo"]
    for phrase in expected:
        assert any(phrase in t for t in titles), phrase
    for item in items:
        assert "- Status: pending" in item
        assert re.search(r"^- Evidence:\s*$", item, flags=re.MULTILINE)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_collect_orders_entries_numerically_and_appends_pending(tmp_path: Path):
    entries = tmp_path / "verification" / "entries"
    for task_id in ["10", "2.10", "1", "2.1", "2.2"]:
        _write(entries / f"{task_id}.md", f"## Task {task_id}\n\nbody {task_id}\n")
    _write(tmp_path / "verification" / "pending.md", "# Pending Items\n\n## P1: x\n")

    text = collector.collect(tmp_path)

    order = re.findall(r"^## Task (\S+)$", text, flags=re.MULTILINE)
    assert order == ["1", "2.1", "2.2", "2.10", "10"]
    assert text.index("# Pending Items") > text.index("## Task 10")
    assert text.startswith("# TapeRescue Verification Log")


def test_collect_with_no_entries_still_includes_pending(tmp_path: Path):
    _write(tmp_path / "verification" / "pending.md", "# Pending Items\n")
    assert "# Pending Items" in collector.collect(tmp_path)


def test_collect_appends_traceability_after_pending(tmp_path: Path):
    _write(tmp_path / "verification" / "entries" / "1.md", "## Task 1\n")
    _write(tmp_path / "verification" / "pending.md", "# Pending Items\n")
    _write(tmp_path / "verification" / "traceability.md", "# Traceability Example\n")
    text = collector.collect(tmp_path)
    assert text.index("## Task 1") < text.index("# Pending Items") < text.index("# Traceability Example")


def test_collect_rejects_non_task_entry_name(tmp_path: Path):
    _write(tmp_path / "verification" / "pending.md", "# Pending Items\n")
    _write(tmp_path / "verification" / "entries" / "notes.md", "x\n")
    with pytest.raises(collector.CollectError, match="notes.md"):
        collector.collect(tmp_path)


def test_main_writes_log_and_fails_without_pending(tmp_path: Path, capsys):
    assert collector.main(["--root", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err

    _write(tmp_path / "verification" / "pending.md", "# Pending Items\n")
    _write(tmp_path / "verification" / "entries" / "1.md", "## Task 1\n")
    assert collector.main(["--root", str(tmp_path)]) == 0
    log = (tmp_path / "VERIFICATION_LOG.md").read_text(encoding="utf-8")
    assert "## Task 1" in log and "# Pending Items" in log
