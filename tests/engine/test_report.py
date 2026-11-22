"""Recovery report (task 13.1): ``taperescue/engine/report.py``.

Requirements 9.13, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import typing
from pathlib import Path

import pytest

from taperescue.crc import crc16_xmodem
from taperescue.engine import RecoverOptions, recover
from taperescue.engine.gate import GATE_REASONS, export_decision
from taperescue.engine.report import (
    ALGORITHM_VERSION,
    REASON_MESSAGES,
    RECONSTRUCTED_LABEL,
    STATUS_LABELS,
    build_report,
    dumps_report,
    loads_report,
)
from taperescue.models import (
    BlockObservation,
    CaptureObservations,
    CrcReading,
    HeaderFields,
    UnresolvedReason,
)

CID_A = "a" * 64
CID_B = "b" * 64
FILENAME = "STARMAP"
LOAD, EXEC = 0xFFFF0E00, 0xFFFF8023

TEST_PAYLOAD = bytes((17 * i + 11 * i * i + 42) % 256 for i in range(256))
TP_CRC = crc16_xmodem(TEST_PAYLOAD)
TAIL = bytes(range(40))


def xor(payload: bytes, flips: dict[int, int]) -> bytes:
    out = bytearray(payload)
    for offset, mask in flips.items():
        out[offset] ^= mask
    return bytes(out)


UNIQUE_A = xor(TEST_PAYLOAD, {23: 0x02, 128: 0x08})
UNIQUE_B = xor(TEST_PAYLOAD, {75: 0x01, 241: 0x10})


def obs(cid, block_no, payload, *, data_outcome="valid", recorded=None,
        expected_len=None, final=False) -> BlockObservation:
    expected_len = len(payload) if expected_len is None else expected_len
    flags = 0x80 if final else 0
    if recorded is None and data_outcome != "unreadable":
        recorded = crc16_xmodem(payload)
    raw = (FILENAME.encode() + b"\x00" + LOAD.to_bytes(4, "little") + EXEC.to_bytes(4, "little")
           + block_no.to_bytes(2, "little") + expected_len.to_bytes(2, "little")
           + bytes([flags]) + bytes(4))
    return BlockObservation(
        capture_id=cid,
        sample_offset=10_000 * (block_no + 1) + (1 if cid == CID_B else 0),
        header_raw=raw,
        header=HeaderFields(FILENAME, LOAD, EXEC, block_no, expected_len, flags, 0),
        header_crc=CrcReading("valid", 0x1234),
        payload=payload,
        expected_len=expected_len,
        actual_len=len(payload),
        data_crc=CrcReading(data_outcome, recorded),
    )


def failed(cid, payload, **kw):
    kw.setdefault("recorded", TP_CRC)
    return obs(cid, 1, payload, data_outcome="invalid", **kw)


def session(mid_a, mid_b):
    """Block 0 intact in both (anchor), block 1 as given, block 2 final and intact."""
    def cap(cid, mid):
        return CaptureObservations(cid, (obs(cid, 0, bytes(256)), mid,
                                         obs(cid, 2, TAIL, final=True)))
    return [cap(CID_A, mid_a), cap(CID_B, mid_b)]


def unique_session():
    return session(failed(CID_A, UNIQUE_A), failed(CID_B, UNIQUE_B))


def report_for(captures, options=RecoverOptions()):
    result = recover(captures, options)
    return build_report(result, export_decision(result))


# --- determinism and serialisation ------------------------------------------


def test_same_input_gives_identical_bytes():
    assert dumps_report(report_for(unique_session())) == dumps_report(report_for(unique_session()))


def test_swapped_capture_order_gives_identical_bytes():
    caps = unique_session()
    assert dumps_report(report_for(caps)) == dumps_report(report_for(list(reversed(caps))))


def test_round_trip_and_canonical_text():
    report = report_for(unique_session())
    text = dumps_report(report)
    assert loads_report(text) == report
    assert dumps_report(loads_report(text)) == text
    assert text == json.dumps(report, sort_keys=True, indent=2)
    with pytest.raises(ValueError):
        loads_report('{"schema": "something/else"}')


def test_decision_defaults_to_gate():
    result = recover(unique_session())
    assert build_report(result) == build_report(result, export_decision(result))


TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
FORBIDDEN_KEYS = {"time", "timestamp", "date", "created", "path", "filename", "upload_order",
                  "display_name", "host", "hostname"}


def assert_no_environment(report: dict, *extra_forbidden: str) -> None:
    text = dumps_report(report)
    assert not TIMESTAMP.search(text)
    for needle in extra_forbidden:
        assert needle not in text

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in FORBIDDEN_KEYS, k
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            assert not node.startswith(("/", "~")) and not re.match(r"[A-Za-z]:\\", node), node
    walk(report)


def test_no_timestamps_or_paths():
    assert_no_environment(report_for(unique_session()), os.getcwd(), str(Path.home()))


# --- report contents ----------------------------------------------------------


def test_top_level_fields():
    caps = unique_session()
    report = report_for(caps + [caps[0]])  # a duplicate of capture A
    assert report["schema"] == "taperescue.report/1"
    assert report["algorithm_version"] == ALGORITHM_VERSION
    assert report["candidate_cap"] == 64
    assert report["reconstruction_enabled"] is True
    assert [i["sha256"] for i in report["inputs"]] == [CID_A, CID_B]
    assert report["duplicates_merged"] == [CID_A]
    assert report["anchors"] == [0, 2]
    assert report["final_block"] == 2
    assert [b["block_no"] for b in report["blocks"]] == [0, 1, 2]
    assert report["export"] == {"permitted": True, "blockers": [], "messages": {},
                                "reconstructed_blocks": [1]}


def test_reconstructed_block_details_for_unique_repair():
    block = report_for(unique_session())["blocks"][1]
    assert block["status"] == "Reconstructed · CRC match"
    assert block["reason"] is None and block["reason_message"] is None
    assert block["label"] == RECONSTRUCTED_LABEL != STATUS_LABELS["Intact"]
    assert "CRC match" in block["label"] and "evidence" in block["label"]
    assert block["supporting_capture_ids"] == [CID_A, CID_B]
    assert block["recorded_data_crc"] == TP_CRC == 0xAC93
    assert block["payload_sha256"] == hashlib.sha256(TEST_PAYLOAD).hexdigest()
    assert [(r["capture_id"], r["sample_offset"], r["data_crc"], r["recorded_data_crc"])
            for r in block["reads"]] == [(CID_A, 20_000, "invalid", TP_CRC),
                                         (CID_B, 20_001, "invalid", TP_CRC)]
    rec = block["reconstruction"]
    assert (rec["attempted"], rec["candidate_count"], rec["evaluated_count"],
            rec["match_count"]) == (True, 16, 16, 1)
    assert [c["offset"] for c in rec["choices"]] == [23, 75, 128, 241]
    expected_source = {23: CID_B, 75: CID_A, 128: CID_B, 241: CID_A}
    for c in rec["choices"]:
        o = c["offset"]
        assert c["observed"] == {CID_A: UNIQUE_A[o], CID_B: UNIQUE_B[o]}
        assert c["chosen"] == TEST_PAYLOAD[o]
        assert c["supporting_capture_ids"] == [expected_source[o]]

    intact = report_for(unique_session())["blocks"][0]
    assert intact["status"] == "Intact" and intact["label"] == STATUS_LABELS["Intact"]
    assert intact["reconstruction"]["attempted"] is False
    assert intact["supporting_capture_ids"] == [CID_A, CID_B]


def test_recovered_digest_only_when_complete():
    report = report_for(unique_session())
    program = bytes(256) + TEST_PAYLOAD + TAIL
    assert report["recovered_payload_sha256"] == hashlib.sha256(program).hexdigest()

    # Requirement 12.2's same-error case: block 1 unresolved, no digest.
    no_match = report_for(session(failed(CID_A, xor(UNIQUE_A, {7: 0x04})),
                                  failed(CID_B, xor(UNIQUE_B, {7: 0x04}))))
    assert "recovered_payload_sha256" not in no_match
    blk = no_match["blocks"][1]
    assert (blk["status"], blk["reason"]) == ("Unresolved", "no_match")
    assert blk["reason_message"] == REASON_MESSAGES["no_match"]
    assert blk["payload_sha256"] is None and blk["supporting_capture_ids"] == []
    rec = blk["reconstruction"]
    assert (rec["candidate_count"], rec["evaluated_count"], rec["match_count"]) == (16, 16, 0)
    assert all(c["chosen"] is None and c["supporting_capture_ids"] == [] for c in rec["choices"])
    assert no_match["export"]["blockers"] == [[1, "no_match"]]
    assert no_match["export"]["messages"] == {"no_match": REASON_MESSAGES["no_match"]}


def test_intact_only_reports_attempted_false():
    report = report_for(unique_session(), RecoverOptions(reconstruct=False))
    assert report["reconstruction_enabled"] is False
    blk = report["blocks"][1]
    assert (blk["status"], blk["reason"]) == ("CRC failed", None)
    assert blk["reason_message"] == REASON_MESSAGES["crc_failed"]
    assert blk["reconstruction"] == {"attempted": False, "candidate_count": None,
                                     "evaluated_count": None, "match_count": None,
                                     "choices": []}
    assert blk["recorded_data_crc"] == TP_CRC
    assert "recovered_payload_sha256" not in report


def test_missing_evidence_has_no_filled_in_bytes_or_crc():
    unreadable = obs(CID_B, 1, UNIQUE_B, data_outcome="unreadable")
    blk = report_for(session(failed(CID_A, UNIQUE_A), unreadable))["blocks"][1]
    assert (blk["status"], blk["reason"]) == ("Unresolved", "crc_unreadable")
    assert blk["recorded_data_crc"] is None and blk["payload_sha256"] is None
    assert blk["reconstruction"]["attempted"] is False
    assert {r["capture_id"]: r["recorded_data_crc"] for r in blk["reads"]} == {
        CID_A: TP_CRC, CID_B: None}

    truncated = failed(CID_B, UNIQUE_B[:200], expected_len=256)
    blk = report_for(session(failed(CID_A, UNIQUE_A), truncated))["blocks"][1]
    assert (blk["reason"], blk["payload_sha256"]) == ("truncated_payload", None)
    assert [r["payload_state"] for r in blk["reads"]] == ["complete", "truncated"]


def test_payload_conflict_reports_conflict_object():
    caps = session(obs(CID_A, 1, TEST_PAYLOAD), obs(CID_B, 1, UNIQUE_B))
    blk = report_for(caps)["blocks"][1]
    assert (blk["status"], blk["reason"]) == ("Conflict", None)
    assert blk["conflict"] == {"kind": "payload", "capture_ids": [CID_A, CID_B]}
    assert blk["reason_message"] == REASON_MESSAGES["payload_conflict"]
    assert blk["payload_sha256"] is None


def test_reason_table_covers_every_code():
    codes = set(typing.get_args(UnresolvedReason)) | set(GATE_REASONS)
    assert codes <= set(REASON_MESSAGES)
    assert set(STATUS_LABELS) == {"Intact", "CRC failed", "Reconstructed · CRC match",
                                  "Unresolved", "Conflict", "Missing"}


def test_report_module_imports_only_engine_and_models():
    tree = ast.parse(Path(__import__("taperescue.engine.report", fromlist=["x"]).__file__)
                     .read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    project = {m for m in modules if m.startswith("taperescue")}
    assert project <= {"taperescue.engine.gate", "taperescue.models"}, project
    assert not {m for m in modules if m.split(".")[0] in {"server", "fastapi", "subprocess", "wave"}}


# --- real WAVs ------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
DECODER = ROOT / "build" / "decoder" / "ScanTape"
FIXTURES = ROOT / "fixtures" / "starmap"


@pytest.mark.skipif(
    not (DECODER.is_file() and os.access(DECODER, os.X_OK)),
    reason=f"patched decoder not built at {DECODER}; run scripts/build_decoder.sh",
)
def test_damaged_fixtures_report(tmp_path):
    from taperescue.decoder_adapter import decode_capture  # test-only import

    reference = json.loads((FIXTURES / "reference_payloads.json").read_text(encoding="utf-8"))
    damage = json.loads((FIXTURES / "damage.json").read_text(encoding="utf-8"))
    caps = [decode_capture(FIXTURES / name, decoder=DECODER, workdir=tmp_path)
            for name in ("damaged_a.wav", "damaged_b.wav")]
    cid_a, cid_b = caps[0].capture_id, caps[1].capture_id

    report = report_for(caps)
    assert dumps_report(report) == dumps_report(report_for(list(reversed(caps))))
    assert_no_environment(report, str(ROOT), str(tmp_path), "damaged_a", "damaged_b", ".wav")
    assert report["recovered_payload_sha256"] == reference["program_sha256"]
    assert report["export"]["permitted"] and report["export"]["reconstructed_blocks"] == [6]

    blk = report["blocks"][6]
    print(json.dumps(blk, sort_keys=True, indent=2))  # recorded in the verification log
    assert blk["status"] == "Reconstructed · CRC match" and blk["label"] == RECONSTRUCTED_LABEL
    assert blk["recorded_data_crc"] == reference["blocks"][6]["data_crc"]
    assert blk["payload_sha256"] == reference["blocks"][6]["sha256"]
    assert {r["capture_id"] for r in blk["reads"]} == {cid_a, cid_b}
    rec = blk["reconstruction"]
    assert (rec["candidate_count"], rec["evaluated_count"], rec["match_count"]) == (16, 16, 1)
    # Each damaged offset is repaired from the other capture.
    expected = {f["offset"]: (f["original"], cid_b) for f in damage["a"]}
    expected.update({f["offset"]: (f["original"], cid_a) for f in damage["b"]})
    assert {c["offset"]: (c["chosen"], c["supporting_capture_ids"][0])
            for c in rec["choices"]} == expected
