"""Run the patched ScanTape decoder on one capture and parse its observations.

The decoder is always started with an argument list and ``shell=False``. Every
path in the list is either the capture path given by the caller or a path this
module generates inside ``workdir`` (Requirement 1.5). User-supplied names are
never interpolated into a command line.

Argument order: the upstream ``ArgParser`` reads the input WAV from ``argv[1]``
and only then parses options, so the WAV comes first::

    ScanTape <wav> -bbm -json <out> -capture-id <sha256-hex>

The patch requires ``-bbm`` with ``-json``, a ``-capture-id`` of exactly 64
lowercase hex digits, a WAV input (UEF and CSW are rejected), and none of the
other output options (``-g``, ``-uef``, ``-wav``, ``-csw``, ``-tap``, ``-ssd``,
``-c``). In ``-json`` mode it writes ``<out>.tmp`` and renames it to ``<out>``
and creates no other files.

Error mapping (all raise :class:`~taperescue.decoder_adapter.schema.DecoderError`):

* ``subprocess.TimeoutExpired`` -> ``timeout``. ``subprocess.run`` kills the
  child before re-raising (Requirement 3.4).
* Decoder cannot be started, or exits non-zero -> ``decoder_failed``. Stderr
  (truncated to 2 KB) goes to the log and to ``detail``, not to the message.
* Missing or non-JSON output file, or a schema violation -> ``invalid_output``.
* No observation with a valid header CRC -> ``no_blocks`` (Requirement 3.5).
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from pathlib import Path

from taperescue.decoder_adapter.schema import DecoderError, parse_observations
from taperescue.models import CaptureObservations

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
NO_BLOCKS_MESSAGE = "No BBC 1200-baud blocks were found in this capture"
_STDERR_LIMIT = 2048
_HASH_CHUNK = 1 << 20


def capture_id_for(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of the file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decoder_args(decoder: Path, wav: Path, out: Path, capture_id: str) -> list[str]:
    """Build the decoder argument list. The WAV must be ``argv[1]``."""
    return [
        str(decoder),
        str(wav),
        "-bbm",
        "-json",
        str(out),
        "-capture-id",
        capture_id,
    ]


def _stderr_text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw[:_STDERR_LIMIT].decode("utf-8", errors="replace")
    return raw[:_STDERR_LIMIT]


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def decode_capture(
    wav: Path,
    *,
    decoder: Path,
    workdir: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CaptureObservations:
    """Decode one WAV capture into validated observations.

    ``out`` is a fresh file name inside ``workdir``; it and the decoder's
    ``<out>.tmp`` are removed before returning. The decoder runs with ``workdir``
    as its working directory.
    """
    wav = Path(wav).resolve()
    decoder = Path(decoder).resolve()
    workdir = Path(workdir).resolve()
    capture_id = capture_id_for(wav)
    out = workdir / f"observations-{uuid.uuid4().hex}.json"
    tmp = out.with_name(out.name + ".tmp")
    args = decoder_args(decoder, wav, out, capture_id)

    try:
        try:
            proc = subprocess.run(
                args,
                shell=False,
                timeout=timeout_s,
                capture_output=True,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise DecoderError(
                "timeout", f"Decoding timed out after {timeout_s:g} s", capture_id
            ) from None
        except OSError as exc:
            log.warning("decoder %s could not be started for capture %s: %s",
                        decoder, capture_id, exc)
            raise DecoderError(
                "decoder_failed", "Decoder failed on this capture", capture_id, str(exc)
            ) from exc

        if proc.returncode != 0:
            # Upstream ScanTape prints some input errors to stdout, so fall
            # back to stdout when stderr is empty.
            output = _stderr_text(proc.stderr).strip() or _stderr_text(proc.stdout).strip()
            log.warning("decoder exited with status %d for capture %s: %s",
                        proc.returncode, capture_id, output)
            raise DecoderError(
                "decoder_failed",
                "Decoder failed on this capture",
                capture_id,
                f"exit status {proc.returncode}: {output}".rstrip(),
            )

        try:
            text = out.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            detail = f"cannot read decoder output file: {exc}"
            raise DecoderError(
                "invalid_output",
                f"Decoder output was invalid for capture {capture_id}: {detail}",
                capture_id,
                detail,
            ) from exc
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            detail = f"decoder output is not JSON: {exc}"
            raise DecoderError(
                "invalid_output",
                f"Decoder output was invalid for capture {capture_id}: {detail}",
                capture_id,
                detail,
            ) from exc
    finally:
        _remove(out)
        _remove(tmp)

    capture = parse_observations(doc, capture_id)
    if not any(obs.header_crc.outcome == "valid" for obs in capture.observations):
        raise DecoderError("no_blocks", NO_BLOCKS_MESSAGE, capture_id)
    return capture
