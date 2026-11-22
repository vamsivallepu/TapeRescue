"""Unit tests for taperescue.wavcheck (Requirements 3.1, 3.2, 3.3)."""

from __future__ import annotations

import io
import struct
import wave
from pathlib import Path

import pytest

from taperescue.wavcheck import (
    MAX_BYTES,
    PCM_SUBFORMAT_GUID,
    WavRejected,
    inspect_wav_header,
    validate_capture,
)

IEEE_FLOAT_GUID = bytes.fromhex("0300000000001000800000aa00389b71")


def stdlib_wav(n_frames: int = 441, rate: int = 44_100, width: int = 2, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * (n_frames * channels * width // 2))
    return buf.getvalue()


def fmt_body(tag=1, channels=1, rate=44_100, bits=16, subformat: bytes | None = None) -> bytes:
    align = channels * bits // 8
    body = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    if subformat is not None:
        body += struct.pack("<HHI", 22, bits, 0x4) + subformat
    return body


def chunk(cid: bytes, body: bytes, declared: int | None = None) -> bytes:
    size = len(body) if declared is None else declared
    return cid + struct.pack("<I", size) + body + (b"\x00" if len(body) & 1 else b"")


def riff(*chunks: bytes) -> bytes:
    payload = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def check(data: bytes, file_size: int | None = None):
    return inspect_wav_header(data, len(data) if file_size is None else file_size)


def rejected(data: bytes, file_size: int | None = None) -> WavRejected:
    with pytest.raises(WavRejected) as exc:
        check(data, file_size)
    return exc.value


# --- accepted captures -------------------------------------------------------


def test_accepts_stdlib_pcm_wav():
    info = check(stdlib_wav(n_frames=44_100))
    assert (info.format_tag, info.sample_rate, info.bits_per_sample, info.channels) == (1, 44_100, 16, 1)
    assert info.data_bytes == 88_200
    assert info.duration_s == pytest.approx(1.0)


def test_accepts_extensible_with_pcm_guid():
    data = riff(chunk(b"fmt ", fmt_body(tag=0xFFFE, subformat=PCM_SUBFORMAT_GUID)), chunk(b"data", b"\x00" * 100))
    info = check(data)
    assert info.format_tag == 0xFFFE and info.data_bytes == 100


def test_walks_past_unknown_and_odd_sized_chunks():
    data = riff(
        chunk(b"LIST", b"abc"),  # odd size, padded
        chunk(b"fmt ", fmt_body()),
        chunk(b"junk", b"\x00" * 10),
        chunk(b"data", b"\x00" * 64),
    )
    assert check(data).data_bytes == 64


def test_header_only_with_real_file_size_is_enough():
    # inspect_wav_header does not need the samples, only the chunk headers.
    header = riff(chunk(b"fmt ", fmt_body())) + b"data" + struct.pack("<I", 88_200 * 600)
    info = check(header, file_size=len(header) + 88_200 * 600)
    assert info.duration_s == pytest.approx(600.0)  # exactly 10 minutes is allowed


def test_validate_capture_reads_file_and_leaves_it_unchanged(tmp_path: Path):
    path = tmp_path / "capture.dat"  # extension is irrelevant
    data = stdlib_wav()
    path.write_bytes(data)
    assert validate_capture(path).data_bytes == 882
    assert path.read_bytes() == data


# --- rejections --------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"", b"RIFF", b"ID3\x04" + b"\x00" * 60, b"RIFF\x00\x00\x00\x00AVI LIST", riff(chunk(b"data", b"\x00" * 8))],
    ids=["empty", "short", "mp3", "avi", "no-fmt"],
)
def test_not_riff_wave(data):
    err = rejected(data)
    assert err.code == "not_riff_wave"
    assert "not a WAV file" in err.message


@pytest.mark.parametrize(
    "body, detected",
    [
        (fmt_body(rate=48_000), "48000 Hz"),
        (fmt_body(bits=8), "8-bit"),
        (fmt_body(bits=24), "24-bit"),
        (fmt_body(channels=2), "2 channels"),
        (fmt_body(tag=3, bits=32), "IEEE float"),
        (fmt_body(tag=0xFFFE, subformat=IEEE_FLOAT_GUID), "non-PCM sub-format"),
        (fmt_body(tag=0xFFFE), "non-PCM sub-format"),  # extensible without the extension
    ],
)
def test_unsupported_format_states_detected_and_supported(body, detected):
    err = rejected(riff(chunk(b"fmt ", body), chunk(b"data", b"\x00" * 16)))
    assert err.code == "unsupported_format"
    assert detected in err.message
    assert "44100 Hz, 16-bit, mono PCM" in err.message


def test_unsupported_format_from_stdlib_stereo_48k():
    err = rejected(stdlib_wav(rate=48_000, channels=2))
    assert err.code == "unsupported_format"
    assert err.message == (
        "Detected PCM, 48000 Hz, 16-bit, 2 channels; TapeRescue supports 44100 Hz, 16-bit, mono PCM"
    )


def test_too_large_is_checked_before_parsing():
    err = rejected(b"not a wav at all", file_size=MAX_BYTES + 1)
    assert err.code == "too_large"
    assert "64 MiB" in err.message and str(MAX_BYTES + 1) in err.message


def test_exactly_64_mib_is_not_too_large():
    header = riff(chunk(b"fmt ", fmt_body())) + b"data" + struct.pack("<I", 1000)
    assert check(header, file_size=MAX_BYTES).data_bytes == 1000


def test_validate_capture_too_large_on_disk(tmp_path: Path):
    path = tmp_path / "big.wav"
    with open(path, "wb") as f:
        f.truncate(MAX_BYTES + 1)  # sparse
    with pytest.raises(WavRejected) as exc:
        validate_capture(path)
    assert exc.value.code == "too_large"


def test_too_long():
    data_bytes = 88_200 * 601  # 601 s, still under 64 MiB
    header = riff(chunk(b"fmt ", fmt_body())) + b"data" + struct.pack("<I", data_bytes)
    err = rejected(header, file_size=len(header) + data_bytes)
    assert err.code == "too_long"
    assert "10 minutes" in err.message and "601.0 s" in err.message


def test_declared_data_size_is_clamped_to_file_size():
    # A data chunk claiming 601 s, but the file holds only 100 bytes of samples.
    header = riff(chunk(b"fmt ", fmt_body())) + b"data" + struct.pack("<I", 88_200 * 601)
    assert check(header + b"\x00" * 100).data_bytes == 100


@pytest.mark.parametrize(
    "data",
    [
        riff(chunk(b"fmt ", fmt_body())),
        riff(chunk(b"fmt ", fmt_body()), chunk(b"data", b"")),
        stdlib_wav(n_frames=0),
    ],
    ids=["no-data-chunk", "empty-data-chunk", "stdlib-zero-frames"],
)
def test_no_data(data):
    err = rejected(data)
    assert err.code == "no_data"
