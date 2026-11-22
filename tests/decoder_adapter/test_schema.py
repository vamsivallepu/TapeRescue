"""Unit tests for the observation schema parser (task 5.1).

Requirements 2.2, 2.4, 2.5, 2.6, 2.7, 2.8.
"""

from __future__ import annotations

import copy

import pytest

from taperescue.decoder_adapter.schema import (
    SCHEMA_ID,
    DecoderError,
    observations_to_doc,
    parse_observations,
)

CID = "3f5a" + "0" * 58 + "e1"
OTHER_CID = "a91c" + "1" * 60


def header(block_no: int = 6, block_len: int = 4, flags: int = 0) -> dict:
    return {
        "filename": "STARMAP",
        "load_addr": 0xFFFF0E00,
        "exec_addr": 0xFFFF8023,
        "block_no": block_no,
        "block_len": block_len,
        "flags": flags,
        "next_addr": 0,
    }


def block(**overrides) -> dict:
    b = {
        "sample_offset": 225117,
        "time_s": 5.1047,
        "header_raw": "535441524d415000",
        "header": header(),
        "header_crc": {"recorded": 41257, "outcome": "valid"},
        "payload": "0d000a1d",
        "expected_len": 4,
        "actual_len": 4,
        "data_crc": {"recorded": 4660, "outcome": "invalid"},
    }
    b.update(overrides)
    return b


def doc(*blocks: dict, capture_id: str = CID) -> dict:
    return {
        "schema": SCHEMA_ID,
        "capture_id": capture_id,
        "decoder": {
            "name": "AtomTapeUtils ScanTape",
            "revision": "98d1a8d1bed00aa22f1d72e3d5e91a254a69ec64",
            "mode": "bbm",
        },
        "blocks": list(blocks) if blocks else [block()],
    }


def reject(d: dict, fragment: str) -> DecoderError:
    with pytest.raises(DecoderError) as info:
        parse_observations(d, CID)
    err = info.value
    assert err.code == "invalid_output"
    assert err.capture_id == CID
    assert CID in err.message
    assert fragment in err.message
    return err


# --- valid documents -------------------------------------------------------


def test_parses_failed_complete_block_with_recorded_crc():
    cap = parse_observations(doc(), CID)
    assert cap.capture_id == CID
    (obs,) = cap.observations
    assert obs.capture_id == CID
    assert obs.sample_offset == 225117
    assert obs.header_raw == bytes.fromhex("535441524d415000")
    assert obs.header is not None and obs.header.filename == "STARMAP"
    assert obs.header.block_no == 6 and obs.header.block_len == 4
    assert obs.header_crc.outcome == "valid" and obs.header_crc.recorded == 41257
    assert obs.payload == bytes.fromhex("0d000a1d")
    assert obs.data_crc is not None
    assert (obs.data_crc.outcome, obs.data_crc.recorded) == ("invalid", 4660)
    assert obs.payload_state == "complete"
    assert not obs.is_intact


def test_intact_block():
    cap = parse_observations(doc(block(data_crc={"outcome": "valid", "recorded": 1})), CID)
    assert cap.observations[0].is_intact


def test_truncated_payload_kept_at_actual_length_without_padding():
    b = block(
        header=header(block_len=256),
        payload="0d000a",
        expected_len=256,
        actual_len=3,
        data_crc={"outcome": "unreadable"},
    )
    obs = parse_observations(doc(b), CID).observations[0]
    assert obs.payload == b"\x0d\x00\x0a"
    assert obs.actual_len == 3 and obs.expected_len == 256
    assert obs.payload_state == "truncated"
    assert obs.data_crc is not None and obs.data_crc.recorded is None


def test_empty_payload_distinct_from_truncated():
    b = block(header=header(block_len=0, flags=0x40), payload="", expected_len=0,
              actual_len=0, data_crc=None)
    obs = parse_observations(doc(b), CID).observations[0]
    assert obs.payload == b""
    assert obs.data_crc is None
    assert obs.payload_state == "empty"
    assert obs.is_intact


def test_unreadable_header_crc_with_null_header():
    b = block(header=None, header_crc={"outcome": "unreadable"})
    obs = parse_observations(doc(b), CID).observations[0]
    assert obs.header is None
    assert obs.header_crc.outcome == "unreadable" and obs.header_crc.recorded is None


def test_invalid_header_crc_drops_parsed_header_fields():
    b = block(header_crc={"outcome": "invalid", "recorded": 7})
    obs = parse_observations(doc(b), CID).observations[0]
    assert obs.header is None
    assert obs.header_crc.recorded == 7


def test_invalid_header_crc_skips_block_len_check():
    # Without a valid header there is no block_len to compare expected_len against.
    b = block(header=None, header_crc={"outcome": "invalid", "recorded": 7},
              payload="00" * 2, expected_len=9, actual_len=2)
    obs = parse_observations(doc(b), CID).observations[0]
    assert obs.payload_state == "truncated"


def test_zero_blocks_and_optional_fields_absent():
    d = doc()
    d["blocks"] = []
    del d["decoder"]
    assert parse_observations(d, CID).observations == ()
    b = block()
    del b["time_s"]
    assert len(parse_observations(doc(b), CID).observations) == 1


def test_block_order_preserved():
    b1 = block(sample_offset=900, header=header(block_no=1))
    b0 = block(sample_offset=100, header=header(block_no=0))
    cap = parse_observations(doc(b1, b0), CID)
    assert [o.sample_offset for o in cap.observations] == [900, 100]


def test_uppercase_hex_accepted():
    obs = parse_observations(doc(block(payload="0D000A1D")), CID).observations[0]
    assert obs.payload == bytes.fromhex("0d000a1d")


def test_round_trip_through_serialiser():
    blocks = [
        block(),
        block(header=None, header_crc={"outcome": "unreadable"}),
        block(header=header(block_len=0), payload="", expected_len=0, actual_len=0,
              data_crc=None),
        block(header=header(block_len=8), payload="ff", expected_len=8, actual_len=1,
              data_crc={"outcome": "unreadable"}),
    ]
    for b in blocks:
        del b["time_s"]
    d = doc(*blocks)
    del d["decoder"]
    cap = parse_observations(d, CID)
    assert observations_to_doc(cap) == d
    assert parse_observations(observations_to_doc(cap), CID) == cap


def test_input_document_not_mutated():
    d = doc()
    before = copy.deepcopy(d)
    parse_observations(d, CID)
    assert d == before


# --- rejection rules -------------------------------------------------------


def test_rejects_wrong_capture_id():
    reject(doc(capture_id=OTHER_CID), "does not match")


def test_rejects_wrong_schema_id():
    d = doc()
    d["schema"] = "taperescue.observations/2"
    reject(d, "schema")


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_rejects_non_object_document(bad):
    reject(bad, "JSON object")


def test_rejects_missing_and_unknown_top_level_keys():
    d = doc()
    del d["blocks"]
    reject(d, "missing blocks")
    d = doc()
    d["extra"] = 1
    reject(d, "unknown key")


def test_rejects_payload_length_differing_from_actual_len():
    reject(doc(block(payload="0d000a")), "holds 3 bytes but actual_len is 4")


def test_rejects_actual_len_above_expected_len():
    b = block(header=header(block_len=2), payload="0d000a1d", expected_len=2, actual_len=4)
    reject(doc(b), "exceeds expected_len")


def test_rejects_expected_len_differing_from_valid_header_block_len():
    b = block(header=header(block_len=256))
    reject(doc(b), "differs from header.block_len")


def test_rejects_recorded_with_unreadable_crc():
    reject(doc(block(data_crc={"outcome": "unreadable", "recorded": 4660})), "must be omitted")
    reject(doc(block(header_crc={"outcome": "unreadable", "recorded": 1})), "must be omitted")


def test_rejects_missing_recorded_for_readable_crc():
    reject(doc(block(data_crc={"outcome": "invalid"})), "recorded is required")
    reject(doc(block(header_crc={"outcome": "valid"})), "recorded is required")


@pytest.mark.parametrize("value", [-1, 0x10000, True, 1.0, "4660"])
def test_rejects_recorded_out_of_range_or_wrong_type(value):
    reject(doc(block(data_crc={"outcome": "invalid", "recorded": value})), "data_crc.recorded")


def test_rejects_unknown_crc_outcome():
    reject(doc(block(data_crc={"outcome": "ok", "recorded": 1})), "outcome")


def test_rejects_valid_data_crc_on_truncated_payload():
    b = block(header=header(block_len=8), expected_len=8, actual_len=4,
              data_crc={"outcome": "valid", "recorded": 1})
    reject(doc(b), "valid but the payload is truncated")


def test_rejects_data_crc_on_zero_length_block():
    b = block(header=header(block_len=0), payload="", expected_len=0, actual_len=0,
              data_crc={"outcome": "unreadable"})
    reject(doc(b), "must be null for a zero-length block")


def test_rejects_missing_data_crc_on_non_empty_block():
    reject(doc(block(data_crc=None)), "data_crc is required")


def test_rejects_valid_header_crc_without_header():
    reject(doc(block(header=None)), "header is required")


@pytest.mark.parametrize("payload", ["0d0", "zz000a1d", "0d 00 0a 1d", 13])
def test_rejects_malformed_hex(payload):
    reject(doc(block(payload=payload)), "hex")


@pytest.mark.parametrize(
    "field,value",
    [
        ("sample_offset", -1),
        ("expected_len", "4"),
        ("actual_len", None),
        ("time_s", -0.5),
        ("time_s", float("nan")),
    ],
)
def test_rejects_bad_block_scalars(field, value):
    reject(doc(block(**{field: value})), field)


@pytest.mark.parametrize(
    "field,value",
    [("filename", "ELEVENCHARS"), ("filename", 5), ("flags", 0x100), ("block_no", -1),
     ("load_addr", 0x1_0000_0000)],
)
def test_rejects_bad_header_fields(field, value):
    h = header()
    h[field] = value
    reject(doc(block(header=h)), f"header.{field}")


def test_rejects_missing_and_unknown_block_keys():
    b = block()
    del b["data_crc"]
    reject(doc(b), "missing data_crc")
    reject(doc(block(padding="00")), "unknown key")


def test_error_names_block_index():
    err = reject(doc(block(), block(payload="00")), "blocks[1]")
    assert err.detail is not None and err.detail.startswith("blocks[1]")
