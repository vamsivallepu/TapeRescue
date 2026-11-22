"""Recovery report: a JSON document describing inputs, per-block results,
evidence, the export decision and the recovered payload digest.

``build_report(result, decision)`` is a pure function over a
:class:`RecoveryResult` and its :class:`ExportDecision`. It imports only the
data models and the export gate; it never touches the decoder, WAV files, the
filesystem or the web layer.

Determinism (Requirement 10.6): the report holds no timestamps, host names,
local paths, upload filenames or upload order. Every list is sorted (blocks by
number, reads by capture ID then sample offset, choices by offset, capture IDs
ascending), and :func:`dumps_report` writes with sorted keys, so the same
captures in any order give byte-identical text. The report contains no
filenames: the cassette filename is tape evidence held in the observations,
and the report does not repeat it.

Evidence rules:

* Reads with an invalid or unreadable header CRC are listed with their
  outcomes and lengths only; their recorded data CRC and payload digest are
  ``null`` because such reads never supply bytes or CRC values.
* A block's ``payload_sha256`` is set only for ``Intact`` and
  ``Reconstructed · CRC match`` blocks. Unrecovered blocks carry no chosen
  bytes (``chosen`` is ``null`` on every offset) and no substitute CRC.
* The block-level ``recorded_data_crc`` is the single value recorded on tape
  by the reads that decided the block (the intact reads for ``Intact``, the
  valid-header reads otherwise). It is ``null`` when those reads carry no data
  CRC, when any of them is unreadable, or when they disagree; the per-read
  values stay visible under ``reads``.
* ``recovered_payload_sha256`` is present only when every block ``0..final``
  is recovered (Requirement 10.5).

:data:`REASON_MESSAGES` is the single table of human-readable text for every
engine reason code and export-gate code, used by the CLI, the API and the UI.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from taperescue.engine.gate import ExportDecision, export_decision
from taperescue.models import BlockObservation, BlockResult, RecoveryResult

__all__ = [
    "ALGORITHM_VERSION",
    "RECONSTRUCTED_LABEL",
    "REASON_MESSAGES",
    "REPORT_SCHEMA",
    "STATUS_LABELS",
    "block_reason_code",
    "build_report",
    "dumps_report",
    "loads_report",
    "reason_message",
]

REPORT_SCHEMA = "taperescue.report/1"
ALGORITHM_VERSION = "taperescue-merge/1.0.0"

RECONSTRUCTED_LABEL = "CRC match is evidence, not proof of the original bytes"

# One label per Block_Status. The Reconstructed label states that the result
# rests on a CRC match, distinct from an Intact block (Requirement 9.13).
STATUS_LABELS: dict[str, str] = {
    "Intact": "Data CRC verified in at least one capture",
    "CRC failed": "Data CRC failed; not recovered",
    "Reconstructed · CRC match": RECONSTRUCTED_LABEL,
    "Unresolved": "Not recovered",
    "Conflict": "Captures disagree; not recovered",
    "Missing": "Not found in any capture",
}

# Engine Unresolved_Reason codes first, then the export-gate codes.
REASON_MESSAGES: dict[str, str] = {
    "no_match": "No combination of the observed bytes matches the recorded data CRC.",
    "multiple_matches": (
        "More than one combination of the observed bytes matches the recorded data CRC, "
        "so the original bytes cannot be identified."
    ),
    "cap_exceeded": (
        "The reads differ at too many offsets: the candidate count exceeds the cap, "
        "so no candidate was evaluated."
    ),
    "crc_unreadable": "The recorded data CRC could not be read in at least one capture.",
    "crc_mismatch_between_reads": "The captures recorded different data CRC values for this block.",
    "truncated_payload": "At least one read of this block ended before its declared length.",
    "header_invalid": "No read of this block has a valid header CRC.",
    "header_disagreement": "Valid headers for this block disagree between reads.",
    "single_read": (
        "Only one usable read of this block exists, so it cannot be cross-checked "
        "or reconstructed."
    ),
    "block_missing": "This block was not found in any capture.",
    "duplicate_block": "More than one result exists for this block number.",
    "crc_failed": "The block failed its data CRC and reconstruction was not run.",
    "payload_conflict": "Captures hold intact copies of this block with different bytes.",
    "unresolved": "The block could not be recovered.",
    "payload_missing": "The block has a recovered status but no payload.",
    "filename_mismatch": "The block's cassette filename differs from the rest of the file.",
    "address_mismatch": (
        "The block's load or execution address differs from the rest of the file."
    ),
    "block_no_mismatch": "The block number in the header differs from the block's position.",
    "unexpected_final_flag": "The final-block flag appears before the end of the file.",
    "final_flag_missing": "The last block's header lacks the final-block flag.",
    "block_len_invalid": "The declared block length is not valid for the block's position.",
    "payload_length_mismatch": "The payload length differs from the length in the header.",
}

_RECOVERED = ("Intact", "Reconstructed · CRC match")
# Code used for the message when the engine left ``reason`` unset; mirrors the
# export gate's fallback so the block entry and the blocker read the same.
_STATUS_FALLBACK = {
    "CRC failed": "crc_failed",
    "Conflict": "payload_conflict",
    "Unresolved": "unresolved",
    "Missing": "block_missing",
}


def reason_message(code: str | None) -> str | None:
    """Human-readable text for a reason or gate code; ``None`` for ``None``."""
    if code is None:
        return None
    return REASON_MESSAGES.get(code, f"Unrecognised reason code {code!r}.")


def block_reason_code(block: BlockResult) -> str | None:
    """The engine reason, or the gate's fallback code for an unrecovered block
    whose reason is unset (intact-only ``CRC failed``, payload ``Conflict``)."""
    if block.reason is not None:
        return block.reason
    if block.status in _RECOVERED:
        return None
    return _STATUS_FALLBACK.get(block.status, "unresolved")


# --- building -----------------------------------------------------------------


def build_report(result: RecoveryResult, decision: ExportDecision | None = None) -> dict:
    """The recovery report as a JSON-ready dict (only dicts, lists, str, int,
    bool and ``None``), so ``loads_report(dumps_report(r)) == r``.

    ``decision`` defaults to ``export_decision(result)``.
    """
    if decision is None:
        decision = export_decision(result)

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "algorithm_version": ALGORITHM_VERSION,
        "candidate_cap": result.options.candidate_cap,
        "reconstruction_enabled": result.options.reconstruct,
        "inputs": [
            {
                "capture_id": cap.capture_id,
                "sha256": cap.capture_id,
                "observation_count": len(cap.observations),
            }
            for cap in sorted(result.captures, key=lambda c: c.capture_id)
        ],
        "duplicates_merged": sorted(result.duplicates_merged),
        "anchors": sorted(result.anchors),
        "final_block": result.final_block,
        "blocks": [_block_entry(b) for b in sorted(result.blocks, key=lambda b: b.block_no)],
        "export": _export_entry(decision),
    }
    payload = result.program_payload()
    if payload is not None:
        report["recovered_payload_sha256"] = _sha256(payload)
    return report


def _block_entry(block: BlockResult) -> dict:
    code = block_reason_code(block)
    recovered = block.status in _RECOVERED and block.payload is not None
    return {
        "block_no": block.block_no,
        "status": block.status,
        "reason": block.reason,
        "reason_message": reason_message(code),
        "label": STATUS_LABELS.get(block.status),
        "supporting_capture_ids": sorted(block.supporting_capture_ids),
        "reads": [
            _read_entry(r)
            for r in sorted(block.reads, key=lambda r: (r.capture_id, r.sample_offset, repr(r)))
        ],
        "recorded_data_crc": _block_recorded_crc(block),
        "reconstruction": _reconstruction_entry(block),
        "conflict": _conflict_entry(block.conflict),
        "payload_sha256": _sha256(block.payload) if recovered else None,  # type: ignore[arg-type]
    }


def _read_entry(r: BlockObservation) -> dict:
    trusted = r.header_crc.outcome == "valid"
    data_crc = r.data_crc
    return {
        "capture_id": r.capture_id,
        "sample_offset": r.sample_offset,
        "header_crc": r.header_crc.outcome,
        "data_crc": data_crc.outcome if data_crc is not None else None,
        "recorded_data_crc": data_crc.recorded if (trusted and data_crc is not None) else None,
        "expected_len": r.expected_len,
        "actual_len": r.actual_len,
        "payload_state": r.payload_state,
        "payload_sha256": _sha256(r.payload) if trusted else None,
    }


def _block_recorded_crc(block: BlockResult) -> int | None:
    if block.status == "Intact":
        deciding = [r for r in block.reads if r.is_intact]
    else:
        deciding = [r for r in block.reads if r.header_crc.outcome == "valid"]
    if not deciding:
        return None
    values = set()
    for r in deciding:
        if r.data_crc is None:
            if r.payload_state == "empty":
                continue  # an Empty_Payload has no data CRC
            return None
        if r.data_crc.outcome == "unreadable":
            return None
        values.add(r.data_crc.recorded)
    return values.pop() if len(values) == 1 else None


def _reconstruction_entry(block: BlockResult) -> dict:
    rec = block.reconstruction
    if rec is None:
        return {
            "attempted": False,
            "candidate_count": None,
            "evaluated_count": None,
            "match_count": None,
            "choices": [],
        }
    return {
        "attempted": True,
        "candidate_count": rec.candidate_count,
        "evaluated_count": rec.evaluated_count,
        "match_count": rec.match_count,
        "choices": [
            {
                "offset": c.offset,
                "observed": _observed(c.observed),
                "chosen": c.chosen,
                "supporting_capture_ids": sorted(c.supporting_capture_ids),
            }
            for c in sorted(rec.choices, key=lambda c: c.offset)
        ],
    }


def _observed(pairs) -> dict[str, int | list[int]]:
    """``{capture_id: value}``. A capture that contributed several reads with
    different values at this offset maps to the sorted list of its values."""
    by_cid: dict[str, set[int]] = {}
    for cid, value in pairs:
        by_cid.setdefault(cid, set()).add(value)
    return {
        cid: (next(iter(vals)) if len(vals) == 1 else sorted(vals))
        for cid, vals in sorted(by_cid.items())
    }


def _conflict_entry(conflict: dict | None) -> dict | None:
    if conflict is None:
        return None
    return {
        "kind": conflict.get("kind"),
        "capture_ids": sorted(conflict.get("capture_ids", ())),
    }


def _export_entry(decision: ExportDecision) -> dict:
    blockers = sorted(decision.blockers)
    return {
        "permitted": decision.permitted,
        "blockers": [[n, code] for n, code in blockers],
        "messages": {code: reason_message(code) for code in sorted({c for _, c in blockers})},
        "reconstructed_blocks": sorted(decision.reconstructed_blocks),
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- serialisation --------------------------------------------------------------


def dumps_report(report: dict) -> str:
    """Canonical JSON text: sorted keys, two-space indent."""
    return json.dumps(report, sort_keys=True, indent=2)


def loads_report(text: str) -> dict:
    """Parse report text; raises ``ValueError`` if it is not a ``taperescue.report/1``
    document."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"not a {REPORT_SCHEMA} document")
    return doc
