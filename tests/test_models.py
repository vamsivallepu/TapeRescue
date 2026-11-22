"""Unit tests for taperescue.models (task 3.1, Requirements 2.2, 2.4, 2.6, 2.7)."""

import dataclasses

import pytest

from taperescue.models import (
    BlockObservation,
    BlockResult,
    CaptureObservations,
    CrcReading,
    HeaderFields,
    RecoverOptions,
    RecoveryResult,
)

CID = "a" * 64


def header(block_no=0, block_len=256, flags=0, next_addr=0):
    return HeaderFields("STARMAP", 0xFFFF0E00, 0xFFFF8023, block_no, block_len, flags, next_addr)


def obs(payload=b"\x01" * 256, expected_len=256, header_crc=None, data_crc=None, hdr=None):
    return BlockObservation(
        capture_id=CID,
        sample_offset=1000,
        header_raw=b"STARMAP\x00",
        header=hdr if hdr is not None else header(block_len=expected_len),
        header_crc=header_crc or CrcReading("valid", 0x1234),
        payload=payload,
        expected_len=expected_len,
        actual_len=len(payload),
        data_crc=data_crc,
    )


def block(n, payload, status="Intact"):
    return BlockResult(n, status, None, payload, (CID,), (), None, None)


def result(blocks, final_block):
    return RecoveryResult((), (), (), final_block, tuple(blocks), RecoverOptions())


def test_identity_key_excludes_next_addr():
    a, b = header(next_addr=0), header(next_addr=0x1900)
    assert a.identity_key() == b.identity_key()
    assert a.identity_key() == ("STARMAP", 0xFFFF0E00, 0xFFFF8023, 0, 256, 0)
    assert header(flags=0x80).identity_key() != a.identity_key()


def test_crc_reading_unreadable_has_no_value():
    assert CrcReading("unreadable", None).recorded is None
    with pytest.raises(ValueError):
        CrcReading("unreadable", 0x1234)
    with pytest.raises(ValueError):
        CrcReading("valid", None)
    with pytest.raises(ValueError):
        CrcReading("invalid", 0x10000)
    with pytest.raises(ValueError):
        CrcReading("bogus", 1)


def test_complete_valid_read_is_intact():
    o = obs(data_crc=CrcReading("valid", 0xAC93))
    assert o.payload_state == "complete"
    assert o.is_intact


def test_complete_read_with_failed_crc_is_not_intact():
    o = obs(data_crc=CrcReading("invalid", 0xAC93))
    assert o.payload_state == "complete"
    assert not o.is_intact
    assert not obs(data_crc=CrcReading("unreadable", None)).is_intact


def test_truncated_read_keeps_only_bytes_read():
    o = obs(payload=b"\x01" * 100, expected_len=256, data_crc=CrcReading("unreadable", None))
    assert o.payload_state == "truncated"
    assert len(o.payload) == 100
    assert not o.is_intact


def test_empty_payload_is_distinct_from_truncated_and_intact():
    o = obs(payload=b"", expected_len=0, data_crc=None)
    assert o.payload_state == "empty"
    assert o.is_intact
    truncated_to_zero = obs(payload=b"", expected_len=256, data_crc=CrcReading("unreadable", None))
    assert truncated_to_zero.payload_state == "truncated"
    assert not truncated_to_zero.is_intact


def test_invalid_header_is_never_intact():
    o = obs(header_crc=CrcReading("invalid", 1), data_crc=CrcReading("valid", 2), hdr=None)
    o = dataclasses.replace(o, header=None)
    assert not o.is_intact
    empty = dataclasses.replace(obs(payload=b"", expected_len=0), header=None,
                                header_crc=CrcReading("unreadable", None))
    assert empty.payload_state != "empty"
    assert not empty.is_intact


def test_padding_and_overlong_payloads_are_rejected():
    with pytest.raises(ValueError):
        BlockObservation(CID, 0, b"", header(), CrcReading("valid", 1),
                         b"\x00" * 256, 256, 100, CrcReading("unreadable", None))
    with pytest.raises(ValueError):
        BlockObservation(CID, 0, b"", header(), CrcReading("valid", 1),
                         b"\x00" * 300, 256, 300, CrcReading("valid", 1))


def test_capture_rejects_foreign_observation():
    o = obs(data_crc=CrcReading("valid", 1))
    assert CaptureObservations(CID, (o,)).observations == (o,)
    with pytest.raises(ValueError):
        CaptureObservations("b" * 64, (o,))


def test_models_are_frozen():
    o = obs(data_crc=CrcReading("valid", 1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        o.payload = b""  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        RecoverOptions().candidate_cap = 1  # type: ignore[misc]


def test_recover_options_defaults():
    assert RecoverOptions() == RecoverOptions(reconstruct=True, candidate_cap=64)


def test_program_payload_concatenates_recovered_blocks():
    r = result([block(0, b"ab"), block(1, b"", "Intact"),
                block(2, b"cd", "Reconstructed · CRC match")], final_block=2)
    assert r.program_payload() == b"abcd"


def test_program_payload_absent_when_incomplete():
    assert result([block(0, b"ab")], final_block=None).program_payload() is None
    assert result([block(0, b"ab")], final_block=1).program_payload() is None
    failed = BlockResult(1, "CRC failed", "single_read", None, (), (), None, None)
    assert result([block(0, b"ab"), failed], final_block=1).program_payload() is None
    unresolved = BlockResult(1, "Unresolved", "no_match", b"zz", (), (), None, None)
    assert result([block(0, b"ab"), unresolved], final_block=1).program_payload() is None
