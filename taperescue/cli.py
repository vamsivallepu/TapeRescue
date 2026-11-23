"""TapeRescue command-line interface (design section 9).

Commands so far:

* ``taperescue fixtures [--out DIR]`` generates the STARMAP fixtures.
* ``taperescue decode CAPTURE.wav [--decoder PATH] [-o OUT.json]`` validates
  one capture, runs the patched decoder on it, and writes the observation JSON
  to stdout or ``OUT.json``.
* ``taperescue recover A.wav [B.wav] [--intact-only] [--report R.json]
  [--uef OUT.uef]`` decodes one or two captures, runs the recovery engine and
  the export gate, and prints each block's status, the offset choices of
  every reconstructed block, and the export decision. ``--report`` writes the
  recovery report (``dumps_report``) whether or not export is permitted
  (Requirement 10.8). With ``--uef`` it lists the reconstructed blocks, then
  writes and verifies the image, or exits 4 with the blockers when the gate
  refuses. Without ``--uef`` a refused export is reported but is not an
  error, so a single capture can be inspected (Requirement 16.2).
  ``--intact-only`` disables reconstruction (the intact-only baseline).
* ``taperescue export --observations A.json [B.json] -o OUT.uef`` does the
  same from observation documents written by ``decode``; the gate must permit
  export.
* ``taperescue verify-uef IMAGE.uef --reference reference_payloads.json``
  reads the image with the independent reader and compares every block and
  the program SHA-256 with the reference.
* ``taperescue demo [--out DIR]`` is the end-to-end recovery in one command
  (Requirement 14.5): it decodes both damaged STARMAP fixtures with the real
  decoder, shows each single capture and the intact-only merge failing on
  block 6, runs full recovery, writes ``report.json`` and ``STARMAP.uef`` to
  ``DIR`` (default ``demo-out``), and verifies the image against
  ``reference_payloads.json``.
* ``taperescue benchmark [--fixtures DIR] [--out DIR]`` decodes the two
  damaged fixtures and compares the best single capture, the intact-block
  merge and TapeRescue reconstruction, plus Quadbike ``lsblocks.php +r`` when
  it is available (Requirement 15). It writes ``results.json`` and
  ``results.md`` to ``DIR`` (default ``benchmarks``).

``recover``, ``export``, ``demo`` and ``benchmark`` hash every input file before and after
the run and fail with exit 11 if a digest changed. Inputs are only opened for
reading (Requirement 10.7). Output paths may not name an input file.

Missing parent directories of an output file (``decode -o``, ``recover
--report/--uef``, ``export -o``, ``demo --out``, ``benchmark --out``,
``fixtures --out``) are created just before that file is written, after the
arguments and the output-versus-input checks have passed. A UEF directory is
created only when the gate permits export. A parent that exists as a regular
file fails with exit 1.

Every failure prints ``error: <reason>`` to stderr and returns a non-zero exit
status (Requirement 16.3). The status identifies the kind of failure:

====  =====================================================================
Code  Meaning
====  =====================================================================
1     Other failure (fixture layout error, output file cannot be written)
2     Command-line usage error
3     Capture rejected: missing, unreadable, or not a supported WAV file
4     Export refused by the gate (``recover --uef`` and ``export``)
5     Decoder timed out
6     Decoder missing, could not start, or exited non-zero
7     Decoder output was invalid (including an invalid observations file)
8     No BBC 1200-baud blocks found in the capture
9     UEF image failed verification (after export, or in ``verify-uef``)
10    Captures rejected as a pair: no anchor block (``PairingRejected``)
11    An input file's SHA-256 changed during the run
====  =====================================================================
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Sequence

from taperescue import __version__

if TYPE_CHECKING:
    from taperescue.engine.gate import ExportDecision
    from taperescue.models import BlockResult, CaptureObservations, RecoverOptions, RecoveryResult

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_CAPTURE_REJECTED = 3
EXIT_EXPORT_REFUSED = 4
EXIT_DECODER_TIMEOUT = 5
EXIT_DECODER_FAILED = 6
EXIT_INVALID_OUTPUT = 7
EXIT_NO_BLOCKS = 8
EXIT_VERIFY_FAILED = 9
EXIT_NO_ANCHOR = 10
EXIT_INPUT_CHANGED = 11

MAX_CAPTURES = 2

DECODER_EXIT_CODES = {
    "timeout": EXIT_DECODER_TIMEOUT,
    "decoder_failed": EXIT_DECODER_FAILED,
    "invalid_output": EXIT_INVALID_OUTPUT,
    "no_blocks": EXIT_NO_BLOCKS,
}

DECODER_ENV = "TAPERESCUE_DECODER"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DECODER = REPO_ROOT / "build" / "decoder" / "ScanTape"
DEFAULT_FIXTURES_OUT = Path("fixtures") / "starmap"
DEFAULT_DEMO_FIXTURES = REPO_ROOT / "fixtures" / "starmap"
DEFAULT_DEMO_OUT = Path("demo-out")


class CliError(Exception):
    """A failure to report as ``error: <message>`` with exit status ``code``."""

    def __init__(self, message: str, code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _Parser(argparse.ArgumentParser):
    """argparse with usage errors reported as ``error: <reason>``."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"error: {message}\n")


# --- fixtures ----------------------------------------------------------------

def _cmd_fixtures(args: argparse.Namespace) -> int:
    from taperescue.fixtures.starmap import StarmapLayoutError, generate

    try:
        result = generate(args.out)
    except StarmapLayoutError as exc:
        raise CliError(f"STARMAP layout: {exc}") from exc
    except OSError as exc:
        raise CliError(f"cannot write fixtures to {args.out}: {exc.strerror or exc}") from exc

    print(f"wrote STARMAP fixtures to {result.out_dir}")
    print(f"program: {len(result.program.program)} bytes in {len(result.program.blocks)} blocks")
    print(f"damage plan: {result.plan.source}")
    for name in sorted(result.wav_sha256):
        print(f"{result.wav_sha256[name]}  {name}")
    return EXIT_OK


# --- decode ------------------------------------------------------------------

def _resolve_decoder(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get(DECODER_ENV)
    if env:
        return Path(env)
    return DEFAULT_DECODER


def _check_decoder(decoder: Path) -> None:
    if not decoder.is_file():
        raise CliError(
            f"decoder not found at {decoder}; run scripts/build_decoder.sh, "
            f"or pass --decoder or set {DECODER_ENV}",
            EXIT_DECODER_FAILED,
        )
    if not os.access(decoder, os.X_OK):
        raise CliError(f"decoder at {decoder} is not executable", EXIT_DECODER_FAILED)


def _validate_capture(wav: Path) -> None:
    from taperescue.wavcheck import WavRejected, validate_capture

    try:
        validate_capture(wav)
    except FileNotFoundError:
        raise CliError(f"capture not found: {wav}", EXIT_CAPTURE_REJECTED) from None
    except IsADirectoryError:
        raise CliError(f"capture is a directory: {wav}", EXIT_CAPTURE_REJECTED) from None
    except OSError as exc:
        raise CliError(
            f"cannot read capture {wav}: {exc.strerror or exc}", EXIT_CAPTURE_REJECTED
        ) from exc
    except WavRejected as exc:
        raise CliError(f"{wav}: {exc.message}", EXIT_CAPTURE_REJECTED) from exc


def _ensure_parent(out: Path) -> None:
    """Create the missing parent directories of an output file.

    Called just before the file is written, after argument validation and the
    output-versus-input checks, so a refused run leaves no directories behind.
    """
    parent = out.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        raise CliError(f"cannot create directory {parent}: it exists and is not a directory"
                       ) from None
    except OSError as exc:
        raise CliError(f"cannot create directory {parent}: {exc.strerror or exc}") from exc


def _write_output(text: str, out: Path) -> None:
    _ensure_parent(out)
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot write {out}: {exc.strerror or exc}") from exc


def _decode_wav(wav: Path, decoder_arg: str | None, timeout_s: float) -> CaptureObservations:
    """Run the decoder on a capture that has already passed ``_validate_capture``."""
    from taperescue.decoder_adapter import DecoderError, decode_capture

    decoder = _resolve_decoder(decoder_arg)
    _check_decoder(decoder)
    try:
        with tempfile.TemporaryDirectory(prefix="taperescue-decode-") as workdir:
            return decode_capture(
                wav, decoder=decoder, workdir=Path(workdir), timeout_s=timeout_s
            )
    except DecoderError as exc:
        raise CliError(exc.message, DECODER_EXIT_CODES.get(exc.code, EXIT_FAILURE)) from exc


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def _cmd_decode(args: argparse.Namespace) -> int:
    from taperescue.decoder_adapter.schema import observations_to_doc

    wav = Path(args.capture)
    _validate_capture(wav)
    if args.output is not None and _same_file(Path(args.output), wav):
        raise CliError("the output file must not be the input capture", EXIT_USAGE)
    capture = _decode_wav(wav, args.decoder, args.timeout)

    text = json.dumps(observations_to_doc(capture), sort_keys=True, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(text)
    else:
        _write_output(text, Path(args.output))
        print(f"wrote {len(capture.observations)} observations for capture "
              f"{capture.capture_id} to {args.output}")
    return EXIT_OK


# --- input integrity -----------------------------------------------------------

def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _inputs_unchanged(paths: Sequence[Path]) -> Iterator[None]:
    """Hash every input file on entry and again on exit (Requirement 10.7).

    A changed or vanished file raises ``CliError`` with exit 11. The check also
    runs when the body fails, so an earlier error cannot hide a modified input.
    Files are only ever opened with mode ``"rb"``.
    """
    before = {p: _file_sha256(p) for p in dict.fromkeys(Path(p) for p in paths)}
    try:
        yield
    finally:
        changed = []
        for path, digest in before.items():
            try:
                after = _file_sha256(path)
            except OSError:
                after = None
            if after != digest:
                changed.append(str(path))
        if changed:
            raise CliError(
                f"input file changed during the run: {', '.join(changed)}", EXIT_INPUT_CHANGED
            )


def _check_outputs(inputs: Sequence[Path], outputs: Mapping[str, Path | None], kind: str) -> None:
    """Output files must not name an input file or each other."""
    named = [(label, p) for label, p in outputs.items() if p is not None]
    for label, out in named:
        if any(_same_file(out, inp) for inp in inputs):
            raise CliError(f"the {label} must not be the input {kind}", EXIT_USAGE)
    for i, (label_a, a) in enumerate(named):
        for label_b, b in named[i + 1:]:
            if _same_file(a, b):
                raise CliError(f"the {label_a} and the {label_b} must be different files",
                               EXIT_USAGE)


def _check_capture_count(names: Sequence[str], what: str) -> None:
    if len(names) > MAX_CAPTURES:
        raise CliError(f"{what} accepts one or two captures (got {len(names)})", EXIT_USAGE)


# --- recover and export ------------------------------------------------------

def _labels(captures: Sequence[CaptureObservations], names: Sequence[str]) -> dict[str, str]:
    """Display name per capture ID for stdout only; the report never holds names.

    File base names are used when they tell the inputs apart, else the paths
    as given.
    """
    if len({Path(n).name for n in names}) == len(set(names)):
        names = [Path(n).name for n in names]
    grouped: dict[str, list[str]] = {}
    for cap, name in zip(captures, names):
        seen = grouped.setdefault(cap.capture_id, [])
        if name not in seen:
            seen.append(name)
    return {cid: " = ".join(v) for cid, v in grouped.items()}


def _name(cid: str, labels: Mapping[str, str]) -> str:
    return labels.get(cid) or cid[:12]


def _status_text(block: BlockResult) -> str:
    return f"{block.status}  ({block.reason})" if block.reason else block.status


def _print_reconstruction(block: BlockResult, labels: Mapping[str, str]) -> None:
    from taperescue.engine.report import STATUS_LABELS

    rec = block.reconstruction
    if rec is None:
        return
    matches = "skipped" if rec.match_count is None else str(rec.match_count)
    print(f"           candidates {rec.candidate_count}, evaluated {rec.evaluated_count}, "
          f"matches {matches}")
    for c in sorted(rec.choices, key=lambda c: c.offset):
        observed = " ".join(f"{_name(cid, labels)}=0x{v:02X}" for cid, v in c.observed)
        line = f"           offset {c.offset:>3}: {observed}"
        if c.chosen is not None:
            sources = ", ".join(_name(cid, labels) for cid in c.supporting_capture_ids)
            line += f" -> chosen 0x{c.chosen:02X} from {sources}"
        print(line)
    if block.status == "Reconstructed · CRC match":
        print(f"           note: {STATUS_LABELS[block.status]}")


def _print_summary(
    result: RecoveryResult, decision: ExportDecision, labels: Mapping[str, str] | None = None
) -> None:
    """Human summary of block statuses and the export decision (stdout)."""
    labels = labels or {}
    for cap in result.captures:
        label = labels.get(cap.capture_id)
        suffix = f" ({label})" if label else ""
        print(f"capture {cap.capture_id}{suffix}: {len(cap.observations)} observations")
    for cid in result.duplicates_merged:
        print(f"duplicate capture merged: {cid}")
    if len(result.captures) > 1:
        print(f"anchors: {', '.join(map(str, result.anchors))}")
    mode = "enabled" if result.options.reconstruct else "disabled (intact-only)"
    print(f"reconstruction: {mode}")
    final = "unknown" if result.final_block is None else str(result.final_block)
    print(f"final block: {final}")
    for block in result.blocks:
        print(f"block {block.block_no:>3}  {_status_text(block)}")
        _print_reconstruction(block, labels)
    payload = result.program_payload()
    if payload is not None:
        print(f"recovered payload sha256: {hashlib.sha256(payload).hexdigest()}")
    if decision.permitted:
        print("export: permitted")
    else:
        print("export: refused")
        for n, reason in decision.blockers:
            print(f"  blocker: block {n}: {reason}")


def _blocker_text(blockers: Sequence[tuple[int, str]]) -> str:
    return ", ".join(f"block {n}: {reason}" for n, reason in blockers)


def _export(result: RecoveryResult, decision: ExportDecision, out: Path) -> None:
    """Write the UEF image, mapping refusals and verification failures to CLI errors.

    Before writing, the reconstructed block numbers are listed, the CLI
    counterpart of the Web UI confirmation (Requirement 11.6).
    """
    from taperescue.engine.report import RECONSTRUCTED_LABEL
    from taperescue.uef.exporter import ExportRefused, export_uef
    from taperescue.uef.verify import ExportVerificationFailed

    if decision.permitted:
        rebuilt = decision.reconstructed_blocks
        if rebuilt:
            print(f"reconstructed blocks in this image: {', '.join(map(str, rebuilt))} "
                  f"({RECONSTRUCTED_LABEL})")
        else:
            print("reconstructed blocks in this image: none")
        # Only a permitted export writes the image, so only then is its
        # directory created. The exporter's temporary file lives there too.
        _ensure_parent(out)
    try:
        export_uef(result, decision, out)
    except ExportRefused as exc:
        raise CliError(
            f"export refused: {_blocker_text(exc.blockers)}", EXIT_EXPORT_REFUSED
        ) from exc
    except ExportVerificationFailed as exc:
        raise CliError(str(exc), EXIT_VERIFY_FAILED) from exc
    except OSError as exc:
        raise CliError(f"cannot write {out}: {exc.strerror or exc}") from exc
    print(f"wrote {out}")


def _run_recovery(
    captures: Sequence[CaptureObservations], options: RecoverOptions
) -> tuple[RecoveryResult, ExportDecision]:
    from taperescue.engine import PairingRejected, recover
    from taperescue.engine.gate import export_decision

    try:
        result = recover(captures, options)
    except PairingRejected as exc:
        raise CliError(exc.message, EXIT_NO_ANCHOR) from exc
    return result, export_decision(result)


def _write_report(result: RecoveryResult, decision: ExportDecision, path: Path) -> None:
    from taperescue.engine.report import build_report, dumps_report

    _write_output(dumps_report(build_report(result, decision)) + "\n", path)
    print(f"wrote report {path}")


def _recover_and_export(
    captures: Sequence[CaptureObservations],
    out: Path | None,
    *,
    options: RecoverOptions | None = None,
    report: Path | None = None,
    labels: Mapping[str, str] | None = None,
) -> int:
    from taperescue.models import RecoverOptions

    result, decision = _run_recovery(captures, options or RecoverOptions())
    _print_summary(result, decision, labels)
    # The report is written before export so a refused export still leaves
    # it behind (Requirement 10.8).
    if report is not None:
        _write_report(result, decision, report)
    if out is not None:
        _export(result, decision, out)
    return EXIT_OK


def _cmd_recover(args: argparse.Namespace) -> int:
    from taperescue.models import RecoverOptions

    _check_capture_count(args.captures, "recover")
    wavs = [Path(c) for c in args.captures]
    for wav in wavs:
        _validate_capture(wav)
    out = None if args.uef is None else Path(args.uef)
    report = None if args.report is None else Path(args.report)
    _check_outputs(wavs, {"UEF output": out, "report": report}, "capture")
    options = RecoverOptions(reconstruct=not args.intact_only)
    with _inputs_unchanged(wavs):
        captures = [_decode_wav(wav, args.decoder, args.timeout) for wav in wavs]
        return _recover_and_export(captures, out, options=options, report=report,
                                   labels=_labels(captures, args.captures))


_CAPTURE_ID_LEN = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


def _load_observations(path: Path) -> CaptureObservations:
    """Load an observation document as written by ``decode`` and validate it."""
    from taperescue.decoder_adapter import DecoderError, parse_observations

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"observations not found: {path}") from None
    except OSError as exc:
        raise CliError(f"cannot read observations {path}: {exc.strerror or exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(f"{path}: not a JSON document: {exc}", EXIT_INVALID_OUTPUT) from exc
    cid = doc.get("capture_id") if isinstance(doc, dict) else None
    if not (isinstance(cid, str) and len(cid) == _CAPTURE_ID_LEN and set(cid) <= _HEX_DIGITS):
        raise CliError(
            f"{path}: capture_id must be {_CAPTURE_ID_LEN} lowercase hex digits",
            EXIT_INVALID_OUTPUT,
        )
    try:
        return parse_observations(doc, cid)
    except DecoderError as exc:
        raise CliError(f"{path}: {exc.message}", EXIT_INVALID_OUTPUT) from exc


def _cmd_export(args: argparse.Namespace) -> int:
    _check_capture_count(args.observations, "export")
    paths = [Path(p) for p in args.observations]
    out = Path(args.output)
    _check_outputs(paths, {"UEF output": out}, "observations file")
    captures = [_load_observations(p) for p in paths]
    with _inputs_unchanged(paths):
        return _recover_and_export(captures, out, labels=_labels(captures, args.observations))


# --- verify-uef ----------------------------------------------------------------

def _load_reference(path: Path) -> dict:
    try:
        ref = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"reference not found: {path}") from None
    except OSError as exc:
        raise CliError(f"cannot read reference {path}: {exc.strerror or exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(f"{path}: not a JSON document: {exc}") from exc
    try:
        blocks = ref["blocks"]
        program_sha256 = ref["program_sha256"]
        parsed = [
            {
                "block_no": int(b["block_no"]),
                "payload": bytes.fromhex(b["hex"]),
                "header_raw": bytes.fromhex(b["header_raw"]) if "header_raw" in b else None,
                "header_crc": b.get("header_crc"),
                "data_crc": b.get("data_crc"),
            }
            for b in blocks
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise CliError(f"{path}: not a reference payload file ({exc})") from exc
    return {"program_sha256": program_sha256, "blocks": parsed}


def _verify_block(fb, ref: dict | None) -> list[str]:
    problems: list[str] = []
    if not fb.header_crc_ok:
        problems.append("header CRC does not match the header bytes")
    if not fb.data_crc_ok:
        problems.append("data CRC does not match the payload")
    if ref is None:
        problems.append("no reference block")
        return problems
    if fb.block_no != ref["block_no"]:
        problems.append(f"block number {fb.block_no}, reference {ref['block_no']}")
    if fb.payload != ref["payload"]:
        problems.append("payload differs from the reference")
    if ref["header_raw"] is not None and fb.header_raw != ref["header_raw"]:
        problems.append("header bytes differ from the reference")
    if ref["header_crc"] is not None and fb.header_crc_in_file != ref["header_crc"]:
        problems.append("header CRC differs from the reference")
    if ref["data_crc"] is not None and fb.data_crc_in_file != ref["data_crc"]:
        problems.append("data CRC differs from the reference")
    return problems


def _verify_against_reference(uef: Path, reference: dict) -> None:
    """Read ``uef`` with the independent reader, print one line per block and
    raise ``CliError`` (exit 9) on any mismatch with ``reference``."""
    from taperescue.uef.reader import UefFormatError, read_uef

    try:
        blocks = read_uef(uef)
    except FileNotFoundError:
        raise CliError(f"UEF image not found: {uef}") from None
    except OSError as exc:
        raise CliError(f"cannot read {uef}: {exc.strerror or exc}") from exc
    except UefFormatError as exc:
        raise CliError(f"{uef}: {exc}", EXIT_VERIFY_FAILED) from exc

    refs = reference["blocks"]
    failed = 0
    for i, fb in enumerate(blocks):
        problems = _verify_block(fb, refs[i] if i < len(refs) else None)
        payload_sha = hashlib.sha256(fb.payload).hexdigest()
        state = "ok" if not problems else "MISMATCH: " + "; ".join(problems)
        print(f"block {fb.block_no:>3}  {len(fb.payload):>3} bytes  "
              f"header_crc 0x{fb.header_crc_in_file:04X}  "
              f"data_crc {'-' if fb.data_crc_in_file is None else f'0x{fb.data_crc_in_file:04X}'}"
              f"  sha256 {payload_sha}  {state}")
        failed += bool(problems)

    program_sha = hashlib.sha256(b"".join(fb.payload for fb in blocks)).hexdigest()
    print(f"payload sha256:   {program_sha}")
    print(f"reference sha256: {reference['program_sha256']}")
    problems = []
    if len(blocks) != len(refs):
        problems.append(f"image holds {len(blocks)} blocks, reference has {len(refs)}")
    if failed:
        problems.append(f"{failed} block(s) differ from the reference")
    if program_sha != reference["program_sha256"]:
        problems.append("payload sha256 differs from the reference program_sha256")
    if problems:
        raise CliError(f"{uef} does not match the reference: " + "; ".join(problems),
                       EXIT_VERIFY_FAILED)
    print(f"verified: {len(blocks)} blocks match the reference")


def _cmd_verify_uef(args: argparse.Namespace) -> int:
    reference = _load_reference(Path(args.reference))
    _verify_against_reference(Path(args.uef), reference)
    return EXIT_OK


# --- demo ----------------------------------------------------------------------

DEMO_CAPTURES = ("damaged_a.wav", "damaged_b.wav")
DEMO_REFERENCE = "reference_payloads.json"
DEMO_UEF = "STARMAP.uef"
DEMO_REPORT = "report.json"


def _demo_expect(ok: bool, what: str) -> None:
    if not ok:
        raise CliError(f"demo: expected {what}")


def _block(result: RecoveryResult, n: int) -> BlockResult | None:
    return next((b for b in result.blocks if b.block_no == n), None)


def _decision_text(decision: ExportDecision) -> str:
    if decision.permitted:
        return "export permitted"
    return f"export refused ({_blocker_text(decision.blockers)})"


def _cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end STARMAP recovery from the two damaged fixtures (Requirement 14)."""
    from taperescue.fixtures.starmap import DAMAGED_BLOCK
    from taperescue.models import RecoverOptions

    fixtures = Path(args.fixtures)
    wavs = [fixtures / name for name in DEMO_CAPTURES]
    reference_path = fixtures / DEMO_REFERENCE
    out_dir = Path(args.out)
    uef, report = out_dir / DEMO_UEF, out_dir / DEMO_REPORT
    for wav in wavs:
        _validate_capture(wav)
    reference = _load_reference(reference_path)
    _check_outputs([*wavs, reference_path], {"UEF output": uef, "report": report}, "fixture")
    # ``out_dir`` is created when the report is written (``_write_output``).

    n = DAMAGED_BLOCK
    with _inputs_unchanged([*wavs, reference_path]):
        print("== decode (patched AtomTapeUtils ScanTape)")
        captures = []
        for name, wav in zip(DEMO_CAPTURES, wavs):
            cap = _decode_wav(wav, args.decoder, args.timeout)
            print(f"{name}: capture {cap.capture_id}, {len(cap.observations)} observations")
            captures.append(cap)
        labels = _labels(captures, DEMO_CAPTURES)

        print("\n== 1. each capture alone")
        for name, cap in zip(DEMO_CAPTURES, captures):
            result, decision = _run_recovery([cap], RecoverOptions())
            block = _block(result, n)
            intact = sum(b.status == "Intact" for b in result.blocks)
            print(f"{name}: block {n} {_status_text(block) if block else 'absent'}; "
                  f"{intact} of {len(result.blocks)} blocks Intact; {_decision_text(decision)}")
            _demo_expect(block is not None and block.status == "CRC failed"
                         and not decision.permitted,
                         f"{name} alone to give block {n} CRC failed and a refused export")

        print("\n== 2. both captures, intact-block selection only")
        result, decision = _run_recovery(captures, RecoverOptions(reconstruct=False))
        block = _block(result, n)
        print(f"block {n}: {_status_text(block) if block else 'absent'} (not recovered); "
              f"{_decision_text(decision)}")
        _demo_expect(block is not None and block.payload is None and not decision.permitted,
                     f"the intact-only merge to leave block {n} unrecovered")

        print("\n== 3. both captures, full recovery")
        result, decision = _run_recovery(captures, RecoverOptions())
        _print_summary(result, decision, labels)
        _write_report(result, decision, report)
        _demo_expect(
            all(b.status == ("Reconstructed · CRC match" if b.block_no == n else "Intact")
                for b in result.blocks),
            f"block {n} Reconstructed · CRC match and every other block Intact",
        )
        _export(result, decision, uef)

        print(f"\n== 4. verify {uef} with the independent UEF reader against {DEMO_REFERENCE}")
        _verify_against_reference(uef, reference)

    print(f"\ndemo complete: block {n} reconstructed from {', '.join(DEMO_CAPTURES)}; "
          f"image {uef}, report {report}")
    return EXIT_OK


# --- benchmark -----------------------------------------------------------------

DEFAULT_BENCHMARK_OUT = Path("benchmarks")
DECODER_REVISION_FILE = REPO_ROOT / "third_party" / "AtomTapeUtils" / "REVISION"


def _decoder_label() -> str:
    try:
        revision = DECODER_REVISION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        revision = "unknown revision"
    return f"AtomTapeUtils ScanTape {revision} with patches/atomtapeutils-json-observations.patch"


def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Baseline benchmark on the two damaged fixtures (Requirement 15)."""
    from taperescue.benchmark import RESULTS_JSON, RESULTS_MD, run_benchmark, write_results
    from taperescue.engine import PairingRejected

    fixtures = Path(args.fixtures)
    wavs = [fixtures / name for name in DEMO_CAPTURES]
    reference_path = fixtures / DEMO_REFERENCE
    out_dir = Path(args.out)
    for wav in wavs:
        _validate_capture(wav)
    reference = _load_reference(reference_path) if reference_path.is_file() else None
    inputs = [*wavs, *([reference_path] if reference else [])]
    _check_outputs(inputs, {"results JSON": out_dir / RESULTS_JSON,
                            "results Markdown": out_dir / RESULTS_MD}, "fixture")

    with _inputs_unchanged(inputs):
        captures = [_decode_wav(wav, args.decoder, args.timeout) for wav in wavs]
        names = _labels(captures, DEMO_CAPTURES)
        try:
            results = run_benchmark(
                captures, names, list(zip(DEMO_CAPTURES, wavs)),
                reference_sha256=reference["program_sha256"] if reference else None,
                decoder=_decoder_label(),
            )
        except PairingRejected as exc:
            raise CliError(exc.message, EXIT_NO_ANCHOR) from exc
    _ensure_parent(out_dir / RESULTS_JSON)
    try:
        json_path, md_path = write_results(results, out_dir)
    except OSError as exc:
        raise CliError(f"cannot write benchmark results to {out_dir}: "
                       f"{exc.strerror or exc}") from exc

    print(results["heading"])
    for mode in results["modes"]:
        ref = mode["matches_reference"]
        ref_text = "" if ref is None else f", matches reference: {'yes' if ref else 'no'}"
        print(f"{mode['label']:<28} {', '.join(mode['captures']):<30} "
              f"recovered {mode['recovered_blocks']} of {mode['required_blocks']}, "
              f"complete program: {'yes' if mode['complete_program'] else 'no'}{ref_text}")
    qb = results["quadbike"]
    if "runs" in qb:
        for run in qb["runs"]:
            if run["ran"]:
                print(f"Quadbike lsblocks.php +r     {run['capture']}: {run['block_rows']} block "
                      f"rows, {run['rows_without_errors']} without errors")
            else:
                print(f"Quadbike lsblocks.php +r     {run['capture']}: not run ({run['reason']})")
    else:
        print(f"Quadbike lsblocks.php +r: not run ({qb['reason']})")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return EXIT_OK


# --- entry point -------------------------------------------------------------

def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    from taperescue.decoder_adapter.run import DEFAULT_TIMEOUT_S

    parser = _Parser(prog="taperescue")
    parser.add_argument("--version", action="version", version=f"taperescue {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("fixtures", help="generate the STARMAP fixtures")
    p.add_argument("--out", type=Path, default=DEFAULT_FIXTURES_OUT,
                   help="output directory (default: fixtures/starmap)")
    p.set_defaults(func=_cmd_fixtures)

    p = sub.add_parser("decode", help="decode one WAV capture to observation JSON")
    p.add_argument("capture", help="44100 Hz, 16-bit, mono PCM WAV capture")
    p.add_argument("--decoder",
                   help=f"patched ScanTape binary (default: ${DECODER_ENV}, "
                        "else build/decoder/ScanTape in the repository)")
    p.add_argument("-o", "--output", help="write the JSON here instead of stdout")
    p.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_S,
                   help=f"decoder time limit in seconds (default: {DEFAULT_TIMEOUT_S:g})")
    p.set_defaults(func=_cmd_decode)

    def add_decoder_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--decoder",
                       help=f"patched ScanTape binary (default: ${DECODER_ENV}, "
                            "else build/decoder/ScanTape in the repository)")
        p.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_S,
                       help=f"decoder time limit in seconds (default: {DEFAULT_TIMEOUT_S:g})")

    p = sub.add_parser("recover", help="recover a program from one or two WAV captures")
    p.add_argument("captures", nargs="+", metavar="CAPTURE",
                   help="44100 Hz, 16-bit, mono PCM WAV capture (one or two)")
    p.add_argument("--intact-only", action="store_true",
                   help="intact-block selection only; do not reconstruct failed blocks")
    p.add_argument("--report", help="write the recovery report JSON here (always written)")
    p.add_argument("--uef", help="write the tape image here if export is permitted")
    add_decoder_options(p)
    p.set_defaults(func=_cmd_recover)

    p = sub.add_parser("export", help="export a UEF image from stored observations")
    p.add_argument("--observations", nargs="+", required=True, metavar="OBS.json",
                   help="observation JSON written by 'taperescue decode' (one or two)")
    p.add_argument("-o", "--output", required=True, help="UEF image to write")
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("verify-uef", help="check a UEF image against reference payloads")
    p.add_argument("uef", help="UEF image to check")
    p.add_argument("--reference", required=True,
                   help="reference_payloads.json from 'taperescue fixtures'")
    p.set_defaults(func=_cmd_verify_uef)

    p = sub.add_parser("demo", help="end-to-end STARMAP recovery from the two damaged fixtures")
    p.add_argument("--out", default=str(DEFAULT_DEMO_OUT),
                   help=f"directory for {DEMO_UEF} and {DEMO_REPORT} (default: demo-out)")
    p.add_argument("--fixtures", default=str(DEFAULT_DEMO_FIXTURES),
                   help="directory holding damaged_a.wav, damaged_b.wav and "
                        "reference_payloads.json (default: fixtures/starmap in the repository)")
    add_decoder_options(p)
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("benchmark",
                       help="compare best single capture, intact-block merge, TapeRescue "
                            "and (when available) Quadbike on the damaged fixtures")
    p.add_argument("--fixtures", default=str(DEFAULT_DEMO_FIXTURES),
                   help="directory holding damaged_a.wav, damaged_b.wav and "
                        "reference_payloads.json (default: fixtures/starmap in the repository)")
    p.add_argument("--out", default=str(DEFAULT_BENCHMARK_OUT),
                   help="directory for results.json and results.md (default: benchmarks)")
    add_decoder_options(p)
    p.set_defaults(func=_cmd_benchmark)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        print("error: no command given", file=sys.stderr)
        return EXIT_USAGE
    try:
        return args.func(args)
    except CliError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
