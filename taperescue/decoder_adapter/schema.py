"""Observation schema ``taperescue.observations/1`` and its parser.

The patched ScanTape decoder writes one JSON document per capture. This module
checks that document against the schema in the design and turns it into
:class:`~taperescue.models.CaptureObservations`. Any violation raises
``DecoderError(code="invalid_output")`` whose message names the capture ID
(Requirement 2.8). Payload bytes are kept exactly as read: no padding is added
and a short read keeps its actual length (Requirements 2.4, 2.5).

Rules enforced, in addition to field types and ranges:

* ``capture_id`` equals the ID the adapter passed to the decoder.
* ``len(bytes.fromhex(payload)) == actual_len`` and ``actual_len <= expected_len``.
* ``expected_len == header.block_len`` when the header CRC is valid.
* A CRC object carries ``recorded`` if and only if ``outcome != "unreadable"``,
  and ``recorded`` is in ``0..0xFFFF`` (Requirement 2.7).
* A ``valid`` data CRC requires ``actual_len == expected_len``.
* ``data_crc`` is ``null`` exactly when ``expected_len == 0`` (Empty_Payload,
  Requirement 2.6); every non-empty block carries a data CRC object.
* A valid header CRC requires a parsed ``header`` object. When the header CRC is
  not valid, any ``header`` object is checked and then dropped, because the
  model only keeps parsed fields from a valid header.
* Unknown keys are rejected so that a mismatch with the decoder patch is caught.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from taperescue.models import (
    CRC_OUTCOMES,
    BlockObservation,
    CaptureObservations,
    CrcReading,
    HeaderFields,
)

SCHEMA_ID = "taperescue.observations/1"

DecoderErrorCode = Literal["timeout", "decoder_failed", "invalid_output", "no_blocks"]

_TOP_REQUIRED = frozenset({"schema", "capture_id", "blocks"})
_TOP_OPTIONAL = frozenset({"decoder"})
_BLOCK_REQUIRED = frozenset(
    {
        "sample_offset",
        "header_raw",
        "header",
        "header_crc",
        "payload",
        "expected_len",
        "actual_len",
        "data_crc",
    }
)
_BLOCK_OPTIONAL = frozenset({"time_s"})
_HEADER_KEYS = frozenset(
    {"filename", "load_addr", "exec_addr", "block_no", "block_len", "flags", "next_addr"}
)
_MAX_FILENAME = 10
_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2})*")


class DecoderError(Exception):
    """A capture that could not be decoded into valid observations.

    ``message`` is user-facing and, for ``invalid_output``, names the capture ID.
    ``detail`` holds the specific rule that failed, when there is one.
    """

    def __init__(
        self,
        code: DecoderErrorCode,
        message: str,
        capture_id: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code: DecoderErrorCode = code
        self.message = message
        self.capture_id = capture_id
        self.detail = detail


class _Invalid(Exception):
    """Internal: one schema rule failed. Converted to DecoderError at the boundary."""


def _invalid(capture_id: str, detail: str) -> DecoderError:
    return DecoderError(
        "invalid_output",
        f"Decoder output was invalid for capture {capture_id}: {detail}",
        capture_id,
        detail,
    )


def _check_keys(obj: dict, required: frozenset, optional: frozenset, where: str) -> None:
    missing = sorted(required - obj.keys())
    if missing:
        raise _Invalid(f"{where} is missing {', '.join(missing)}")
    unknown = sorted(k for k in obj.keys() - required - optional)
    if unknown:
        raise _Invalid(f"{where} has unknown key(s) {', '.join(map(str, unknown))}")


def _int(value: Any, where: str, lo: int, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid(f"{where} must be an integer")
    if value < lo or (hi is not None and value > hi):
        bound = f"{lo}..{hi:#x}" if hi is not None else f">= {lo}"
        raise _Invalid(f"{where} {value} is outside {bound}")
    return value


def _hex(value: Any, where: str) -> bytes:
    if not isinstance(value, str) or _HEX_RE.fullmatch(value) is None:
        raise _Invalid(f"{where} must be a string of hex byte pairs")
    return bytes.fromhex(value)


def _parse_crc(value: Any, where: str) -> CrcReading:
    if not isinstance(value, dict):
        raise _Invalid(f"{where} must be an object")
    if "outcome" not in value:
        raise _Invalid(f"{where} is missing outcome")
    unknown = sorted(k for k in value.keys() - {"outcome", "recorded"})
    if unknown:
        raise _Invalid(f"{where} has unknown key(s) {', '.join(map(str, unknown))}")
    outcome = value["outcome"]
    if outcome not in CRC_OUTCOMES:
        raise _Invalid(f"{where}.outcome {outcome!r} is not one of {', '.join(CRC_OUTCOMES)}")
    if outcome == "unreadable":
        if "recorded" in value:
            raise _Invalid(f"{where}.recorded must be omitted when the CRC is unreadable")
        return CrcReading("unreadable", None)
    if "recorded" not in value:
        raise _Invalid(f"{where}.recorded is required when the outcome is {outcome}")
    recorded = _int(value["recorded"], f"{where}.recorded", 0, 0xFFFF)
    return CrcReading(outcome, recorded)


def _parse_header(value: Any, where: str) -> HeaderFields:
    if not isinstance(value, dict):
        raise _Invalid(f"{where} must be an object or null")
    _check_keys(value, _HEADER_KEYS, frozenset(), where)
    filename = value["filename"]
    if not isinstance(filename, str) or len(filename) > _MAX_FILENAME:
        raise _Invalid(f"{where}.filename must be a string of at most {_MAX_FILENAME} characters")
    return HeaderFields(
        filename=filename,
        load_addr=_int(value["load_addr"], f"{where}.load_addr", 0, 0xFFFFFFFF),
        exec_addr=_int(value["exec_addr"], f"{where}.exec_addr", 0, 0xFFFFFFFF),
        block_no=_int(value["block_no"], f"{where}.block_no", 0, 0xFFFF),
        block_len=_int(value["block_len"], f"{where}.block_len", 0, 0xFFFF),
        flags=_int(value["flags"], f"{where}.flags", 0, 0xFF),
        next_addr=_int(value["next_addr"], f"{where}.next_addr", 0, 0xFFFFFFFF),
    )


def _parse_block(raw: Any, index: int, capture_id: str) -> BlockObservation:
    where = f"blocks[{index}]"
    if not isinstance(raw, dict):
        raise _Invalid(f"{where} must be an object")
    _check_keys(raw, _BLOCK_REQUIRED, _BLOCK_OPTIONAL, where)

    sample_offset = _int(raw["sample_offset"], f"{where}.sample_offset", 0)
    if "time_s" in raw:
        t = raw["time_s"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not t >= 0:
            raise _Invalid(f"{where}.time_s must be a non-negative number")

    header_raw = _hex(raw["header_raw"], f"{where}.header_raw")
    header_crc = _parse_crc(raw["header_crc"], f"{where}.header_crc")
    header = None if raw["header"] is None else _parse_header(raw["header"], f"{where}.header")
    if header_crc.outcome == "valid":
        if header is None:
            raise _Invalid(f"{where}.header is required when the header CRC is valid")
    else:
        header = None

    payload = _hex(raw["payload"], f"{where}.payload")
    expected_len = _int(raw["expected_len"], f"{where}.expected_len", 0)
    actual_len = _int(raw["actual_len"], f"{where}.actual_len", 0)
    if len(payload) != actual_len:
        raise _Invalid(
            f"{where}.payload holds {len(payload)} bytes but actual_len is {actual_len}"
        )
    if actual_len > expected_len:
        raise _Invalid(f"{where}.actual_len {actual_len} exceeds expected_len {expected_len}")
    if header is not None and expected_len != header.block_len:
        raise _Invalid(
            f"{where}.expected_len {expected_len} differs from header.block_len {header.block_len}"
        )

    if expected_len == 0:
        if raw["data_crc"] is not None:
            raise _Invalid(f"{where}.data_crc must be null for a zero-length block")
        data_crc = None
    else:
        if raw["data_crc"] is None:
            raise _Invalid(f"{where}.data_crc is required for a block with expected_len > 0")
        data_crc = _parse_crc(raw["data_crc"], f"{where}.data_crc")
        if data_crc.outcome == "valid" and actual_len != expected_len:
            raise _Invalid(
                f"{where}.data_crc is valid but the payload is truncated "
                f"({actual_len} of {expected_len} bytes)"
            )

    try:
        return BlockObservation(
            capture_id=capture_id,
            sample_offset=sample_offset,
            header_raw=header_raw,
            header=header,
            header_crc=header_crc,
            payload=payload,
            expected_len=expected_len,
            actual_len=actual_len,
            data_crc=data_crc,
        )
    except ValueError as exc:  # defensive: the checks above should already cover these
        raise _Invalid(f"{where}: {exc}") from exc


def parse_observations(doc: Any, expected_capture_id: str) -> CaptureObservations:
    """Validate a decoder observation document and build ``CaptureObservations``.

    Raises ``DecoderError(code="invalid_output")`` naming ``expected_capture_id``
    when any rule fails. Blocks keep the order the decoder emitted them in.
    """
    try:
        if not isinstance(doc, dict):
            raise _Invalid("document must be a JSON object")
        _check_keys(doc, _TOP_REQUIRED, _TOP_OPTIONAL, "document")
        if doc["schema"] != SCHEMA_ID:
            raise _Invalid(f"schema {doc['schema']!r} is not {SCHEMA_ID!r}")
        if doc["capture_id"] != expected_capture_id:
            raise _Invalid(
                f"capture_id {doc['capture_id']!r} does not match {expected_capture_id!r}"
            )
        if "decoder" in doc and not isinstance(doc["decoder"], dict):
            raise _Invalid("decoder must be an object")
        blocks = doc["blocks"]
        if not isinstance(blocks, list):
            raise _Invalid("blocks must be a list")
        observations = tuple(
            _parse_block(raw, i, expected_capture_id) for i, raw in enumerate(blocks)
        )
        return CaptureObservations(capture_id=expected_capture_id, observations=observations)
    except _Invalid as exc:
        raise _invalid(expected_capture_id, str(exc)) from None
    except ValueError as exc:  # defensive: model constructors
        raise _invalid(expected_capture_id, str(exc)) from exc


def _crc_to_doc(crc: CrcReading) -> dict:
    if crc.outcome == "unreadable":
        return {"outcome": "unreadable"}
    return {"outcome": crc.outcome, "recorded": crc.recorded}


def observations_to_doc(capture: CaptureObservations) -> dict:
    """Serialise ``CaptureObservations`` back to a schema document.

    The inverse of :func:`parse_observations` for documents without the optional
    ``decoder`` and ``time_s`` fields. Hex is written in lowercase.
    """
    blocks = []
    for obs in capture.observations:
        h = obs.header
        blocks.append(
            {
                "sample_offset": obs.sample_offset,
                "header_raw": obs.header_raw.hex(),
                "header": None
                if h is None
                else {
                    "filename": h.filename,
                    "load_addr": h.load_addr,
                    "exec_addr": h.exec_addr,
                    "block_no": h.block_no,
                    "block_len": h.block_len,
                    "flags": h.flags,
                    "next_addr": h.next_addr,
                },
                "header_crc": _crc_to_doc(obs.header_crc),
                "payload": obs.payload.hex(),
                "expected_len": obs.expected_len,
                "actual_len": obs.actual_len,
                "data_crc": None if obs.data_crc is None else _crc_to_doc(obs.data_crc),
            }
        )
    return {"schema": SCHEMA_ID, "capture_id": capture.capture_id, "blocks": blocks}
