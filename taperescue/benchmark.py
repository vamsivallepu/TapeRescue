"""Baseline benchmark (design section 10, Requirement 15).

Three in-process modes run on the same decoded captures:

* ``best_single_capture``: each capture alone; the one with the most
  ``Intact`` blocks is reported, ties going to the lowest capture ID.
* ``intact_only_merge``: both captures, intact-block selection only
  (``RecoverOptions(reconstruct=False)``).
* ``taperescue``: both captures with bounded reconstruction.

Each mode reports the number of recovered blocks (``Intact`` or
``Reconstructed · CRC match``) among the required blocks, whether a complete
program was produced (every required block recovered and the export gate
permits export), and, when a reference SHA-256 is given, whether the
recovered program equals the reference.

Quadbike comparison (Requirement 15.2, 15.3). ``lsblocks.php`` reads CSW or
TIBET files, not WAV (Quadbike manual, section 9), so each WAV is first
transcribed with the Quadbike executable and the result is passed to
``php -f lsblocks.php +r``. Both run with argument lists and ``shell=False``.
The comparison runs only when ``QUADBIKE_LSBLOCKS`` names ``lsblocks.php``,
``php`` is on ``PATH``, and ``QUADBIKE_BIN`` names the Quadbike executable.
Otherwise the result is ``{"ran": false, "reason": ...}`` and nothing is
invented.

Results hold no timestamps or absolute paths, so the same inputs give
byte-identical ``results.json`` and ``results.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from taperescue import __version__
from taperescue.engine import recover
from taperescue.engine.gate import export_decision
from taperescue.engine.report import ALGORITHM_VERSION
from taperescue.models import CaptureObservations, RecoverOptions, RecoveryResult

__all__ = [
    "BENCHMARK_SCHEMA",
    "CONTRIBUTION",
    "HEADING",
    "ModeResult",
    "best_single_capture",
    "dumps_results",
    "quadbike_comparison",
    "render_markdown",
    "run_benchmark",
    "run_modes",
    "write_results",
]

BENCHMARK_SCHEMA = "taperescue.benchmark/1"
HEADING = "Inputs: generated STARMAP fixtures"
CONTRIBUTION = "reconstruction across captures with byte provenance"
RESULTS_JSON = "results.json"
RESULTS_MD = "results.md"

LSBLOCKS_ENV = "QUADBIKE_LSBLOCKS"
QUADBIKE_BIN_ENV = "QUADBIKE_BIN"
QUADBIKE_TIMEOUT_S = 300.0

_RECOVERED = ("Intact", "Reconstructed · CRC match")

MODE_LABELS = {
    "best_single_capture": "Best single capture",
    "intact_only_merge": "Intact-block merge",
    "taperescue": "TapeRescue reconstruction",
}


@dataclass(frozen=True)
class ModeResult:
    mode: str
    captures: tuple[str, ...]          # display names, in capture-ID order
    recovered_blocks: int
    required_blocks: int
    complete_program: bool
    matches_reference: bool | None     # None without a reference or a complete program
    program_sha256: str | None
    reconstructed_blocks: tuple[int, ...]
    unrecovered: tuple[tuple[int, str, str | None], ...]   # (block_no, status, reason)

    def to_doc(self) -> dict:
        return {
            "mode": self.mode,
            "label": MODE_LABELS[self.mode],
            "captures": list(self.captures),
            "recovered_blocks": self.recovered_blocks,
            "required_blocks": self.required_blocks,
            "complete_program": self.complete_program,
            "matches_reference": self.matches_reference,
            "program_sha256": self.program_sha256,
            "reconstructed_blocks": list(self.reconstructed_blocks),
            "unrecovered": [
                {"block_no": n, "status": s, "reason": r} for n, s, r in self.unrecovered
            ],
        }


def _required_range(result: RecoveryResult) -> range:
    if result.final_block is not None:
        return range(result.final_block + 1)
    return range(max((b.block_no for b in result.blocks), default=-1) + 1)


def _mode_result(
    mode: str,
    result: RecoveryResult,
    names: Mapping[str, str],
    reference_sha256: str | None,
) -> ModeResult:
    decision = export_decision(result)
    by_no = {b.block_no: b for b in result.blocks}
    required = _required_range(result)
    recovered = 0
    unrecovered: list[tuple[int, str, str | None]] = []
    for n in required:
        block = by_no.get(n)
        if block is not None and block.status in _RECOVERED and block.payload is not None:
            recovered += 1
        elif block is None:
            unrecovered.append((n, "Missing", "block_missing"))
        else:
            unrecovered.append((n, block.status, block.reason))
    payload = result.program_payload()
    complete = payload is not None and decision.permitted
    sha = hashlib.sha256(payload).hexdigest() if complete else None
    # Compared only when a program was produced and a reference was given.
    matches = None if reference_sha256 is None or sha is None else (sha == reference_sha256)
    return ModeResult(
        mode=mode,
        captures=tuple(names.get(c.capture_id, c.capture_id) for c in result.captures),
        recovered_blocks=recovered,
        required_blocks=len(required),
        complete_program=complete,
        matches_reference=matches,
        program_sha256=sha,
        reconstructed_blocks=decision.reconstructed_blocks,
        unrecovered=tuple(unrecovered),
    )


def _intact_count(result: RecoveryResult) -> int:
    return sum(b.status == "Intact" for b in result.blocks)


def best_single_capture(
    captures: Sequence[CaptureObservations],
) -> tuple[CaptureObservations, RecoveryResult]:
    """The capture with the most ``Intact`` blocks alone; ties go to the lowest ID."""
    if not captures:
        raise ValueError("at least one capture is required")
    runs = [(cap, recover([cap], RecoverOptions())) for cap in captures]
    return min(runs, key=lambda r: (-_intact_count(r[1]), r[0].capture_id))


def run_modes(
    captures: Sequence[CaptureObservations],
    names: Mapping[str, str],
    reference_sha256: str | None = None,
) -> list[ModeResult]:
    """Run the three modes. ``names`` maps capture ID to a display name.

    Raises ``PairingRejected`` if the captures share no anchor block.
    """
    _, single = best_single_capture(captures)
    intact_only = recover(captures, RecoverOptions(reconstruct=False))
    full = recover(captures, RecoverOptions())
    return [
        _mode_result("best_single_capture", single, names, reference_sha256),
        _mode_result("intact_only_merge", intact_only, names, reference_sha256),
        _mode_result("taperescue", full, names, reference_sha256),
    ]


# --- Quadbike ------------------------------------------------------------------

_BLOCK_ROW = re.compile(r"^#\s*\d+\s+\[")
_ERROR_TOKEN = re.compile(r"\b(HC|DC|DI)\b")
_METRICS = re.compile(r"^M: Metrics\|Errors\|(\d+)\|Blocks\|(\d+)\s*$")


def parse_lsblocks(stdout: str) -> dict:
    """Count block rows in ``lsblocks.php`` output.

    A row starts ``#  <id> [<sample>]``. A row is counted as recovered when it
    carries none of the error codes ``HC`` (header CRC), ``DC`` (data CRC) or
    ``DI`` (incomplete data). The summary ``M: Metrics|Errors|E|Blocks|N`` line
    is recorded as printed.
    """
    rows = [line.rstrip() for line in stdout.splitlines() if _BLOCK_ROW.match(line)]
    ok = [r for r in rows if not _ERROR_TOKEN.search(r.split("]", 1)[-1])]
    metrics = None
    for line in stdout.splitlines():
        m = _METRICS.match(line.strip())
        if m:
            metrics = {"errors": int(m.group(1)), "blocks": int(m.group(2))}
    return {
        "block_rows": len(rows),
        "rows_without_errors": len(ok),
        "metrics": metrics,
        "rows": rows,
    }


def _quadbike_missing(env: Mapping[str, str], which: Callable[[str], str | None]) -> list[str]:
    missing: list[str] = []
    if which("php") is None:
        missing.append("php is not on PATH")
    lsblocks = env.get(LSBLOCKS_ENV)
    if not lsblocks:
        missing.append(f"{LSBLOCKS_ENV} is not set")
    elif not Path(lsblocks).is_file():
        missing.append(f"{LSBLOCKS_ENV} does not name a file")
    qb = env.get(QUADBIKE_BIN_ENV)
    if not qb:
        missing.append(f"{QUADBIKE_BIN_ENV} is not set")
    elif not (Path(qb).is_file() and os.access(qb, os.X_OK)):
        missing.append(f"{QUADBIKE_BIN_ENV} does not name an executable file")
    return missing


def quadbike_comparison(
    wavs: Sequence[tuple[str, Path]],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    timeout_s: float = QUADBIKE_TIMEOUT_S,
) -> dict:
    """Run Quadbike and ``lsblocks.php +r`` on each WAV, or say why not."""
    env = os.environ if env is None else env
    missing = _quadbike_missing(env, which)
    if missing:
        return {
            "ran": False,
            "reason": (
                "; ".join(missing) + ". lsblocks.php reads CSW or TIBET files, not WAV, "
                "so the comparison needs php, lsblocks.php and the Quadbike transcriber"
            ),
        }

    php = which("php")
    lsblocks = env[LSBLOCKS_ENV]
    quadbike = env[QUADBIKE_BIN_ENV]
    runs = []
    for name, wav in wavs:
        with tempfile.TemporaryDirectory(prefix="taperescue-quadbike-") as tmp:
            tibet = Path(tmp) / "capture.tibet"
            transcribe = [str(quadbike), "-t", str(tibet), str(Path(wav).resolve())]
            listing = [str(php), "-f", str(lsblocks), "+r", str(tibet)]
            try:
                t = subprocess.run(transcribe, shell=False, capture_output=True,
                                   timeout=timeout_s, cwd=tmp)
                if t.returncode != 0 or not tibet.is_file():
                    runs.append({"capture": name, "ran": False,
                                 "reason": f"quadbike exited with status {t.returncode}"})
                    continue
                r = subprocess.run(listing, shell=False, capture_output=True,
                                   timeout=timeout_s, cwd=tmp)
            except subprocess.TimeoutExpired:
                runs.append({"capture": name, "ran": False,
                             "reason": f"timed out after {timeout_s:g} s"})
                continue
            except OSError as exc:
                runs.append({"capture": name, "ran": False,
                             "reason": f"could not start: {exc.strerror or exc}"})
                continue
            stdout = r.stdout.decode("utf-8", errors="replace")
            runs.append({
                "capture": name,
                "ran": True,
                "exit_status": r.returncode,
                "command": ["php", "-f", "lsblocks.php", "+r", "capture.tibet"],
                **parse_lsblocks(stdout),
            })
    return {"ran": any(run["ran"] for run in runs), "runs": runs}


# --- results -------------------------------------------------------------------

def run_benchmark(
    captures: Sequence[CaptureObservations],
    names: Mapping[str, str],
    wavs: Sequence[tuple[str, Path]],
    *,
    reference_sha256: str | None = None,
    decoder: str | None = None,
    quadbike: dict | None = None,
) -> dict:
    """Build the results document. ``quadbike`` defaults to a fresh comparison."""
    modes = run_modes(captures, names, reference_sha256)
    if quadbike is None:
        quadbike = quadbike_comparison(wavs)
    inputs = sorted(
        ({"capture": names.get(c.capture_id, c.capture_id), "sha256": c.capture_id}
         for c in captures),
        key=lambda i: i["sha256"],
    )
    return {
        "heading": HEADING,
        "schema": BENCHMARK_SCHEMA,
        "contribution": CONTRIBUTION,
        "taperescue_version": __version__,
        "algorithm_version": ALGORITHM_VERSION,
        "candidate_cap": RecoverOptions().candidate_cap,
        "decoder": decoder,
        "inputs": inputs,
        "reference_program_sha256": reference_sha256,
        "modes": [m.to_doc() for m in modes],
        "quadbike": quadbike,
    }


def dumps_results(results: dict) -> str:
    return json.dumps(results, indent=2, ensure_ascii=False) + "\n"


def _yes_no(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def _unrecovered_text(mode: dict) -> str:
    items = [
        f"{u['block_no']} ({u['status']}{', ' + u['reason'] if u['reason'] else ''})"
        for u in mode["unrecovered"]
    ]
    return ", ".join(items) or "none"


def render_markdown(results: dict) -> str:
    lines = [
        f"# {results['heading']}",
        "",
        "Baseline benchmark for TapeRescue. The inputs are the generated STARMAP "
        "fixtures, not real archival captures:",
        "",
    ]
    for i in results["inputs"]:
        lines.append(f"- `{i['capture']}` sha256 `{i['sha256']}`")
    lines += [
        "",
        f"Engine `{results['algorithm_version']}`, candidate cap {results['candidate_cap']}, "
        f"decoder {results['decoder'] or 'not recorded'}. "
        f"Reference program sha256 `{results['reference_program_sha256'] or 'not given'}`.",
        "",
        "| Mode | Captures | Recovered blocks | Complete program | Matches reference "
        "| Unrecovered blocks |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for m in results["modes"]:
        lines.append(
            f"| {m['label']} | {', '.join(m['captures'])} "
            f"| {m['recovered_blocks']} of {m['required_blocks']} "
            f"| {_yes_no(m['complete_program'])} | {_yes_no(m['matches_reference'])} "
            f"| {_unrecovered_text(m)} |"
        )
    lines += ["", "## Quadbike `lsblocks.php +r`", ""]
    qb = results["quadbike"]
    if "runs" not in qb:
        lines.append(f"Not run: {qb['reason']}.")
        lines.append("")
        lines.append("No Quadbike result is claimed. The comparison is listed as pending item "
                     "P4 in `verification/pending.md`.")
    else:
        lines += ["| Capture | Ran | Block rows | Rows without errors | Metrics line |",
                  "| --- | --- | --- | --- | --- |"]
        for run in qb["runs"]:
            if run["ran"]:
                met = run["metrics"]
                met_text = "none" if met is None else (
                    f"Errors {met['errors']}, Blocks {met['blocks']}")
                lines.append(f"| {run['capture']} | yes (exit {run['exit_status']}) "
                             f"| {run['block_rows']} | {run['rows_without_errors']} "
                             f"| {met_text} |")
            else:
                lines.append(f"| {run['capture']} | no: {run['reason']} | | | |")
    lines += [
        "",
        "## Contribution",
        "",
        f"TapeRescue's contribution is {results['contribution']}. A block that fails its "
        "data CRC in every capture is rebuilt only from bytes observed at each offset, only "
        "when exactly one combination matches the recorded CRC, and the report names the "
        "capture that supplied each chosen byte. A CRC match is evidence, not proof of the "
        "original bytes.",
        "",
    ]
    return "\n".join(lines)


def write_results(results: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / RESULTS_JSON, out_dir / RESULTS_MD
    json_path.write_text(dumps_results(results), encoding="utf-8")
    md_path.write_text(render_markdown(results), encoding="utf-8")
    return json_path, md_path
