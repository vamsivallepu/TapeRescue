"""Capture validation: check that a file is a 44.1 kHz, 16-bit, mono PCM WAV.

The RIFF chunk list is walked directly from the file contents. The file
extension is never consulted. Requirements 3.1, 3.2, 3.3.
"""

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 600

SUPPORTED_RATE = 44_100
SUPPORTED_BITS = 16
SUPPORTED_CHANNELS = 1
SUPPORTED_TEXT = "TapeRescue supports 44100 Hz, 16-bit, mono PCM"

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_PCM, 00000001-0000-0010-8000-00aa00389b71, as stored on disk.
PCM_SUBFORMAT_GUID = bytes.fromhex("0100000000001000800000aa00389b71")

_FORMAT_NAMES = {
    0x0001: "PCM",
    0x0002: "Microsoft ADPCM",
    0x0003: "IEEE float",
    0x0006: "A-law",
    0x0007: "mu-law",
    0x0011: "IMA ADPCM",
    0x0055: "MPEG Layer 3",
}

# Only the first 40 bytes of a fmt chunk are needed (16 base + 2 cbSize + 22 extension).
_FMT_READ_LIMIT = 40

RejectCode = Literal["not_riff_wave", "unsupported_format", "too_large", "too_long", "no_data"]


@dataclass(frozen=True)
class WavInfo:
    format_tag: int
    sample_rate: int
    bits_per_sample: int
    channels: int
    data_bytes: int
    duration_s: float


class WavRejected(Exception):
    """A capture that TapeRescue cannot accept. ``message`` is user-facing."""

    def __init__(self, code: RejectCode, message: str) -> None:
        super().__init__(message)
        self.code: RejectCode = code
        self.message = message


@dataclass(frozen=True)
class _Fmt:
    format_tag: int
    channels: int
    sample_rate: int
    block_align: int
    bits_per_sample: int
    subformat: bytes | None  # set only for WAVE_FORMAT_EXTENSIBLE with a full extension


def _check_size(file_size: int) -> None:
    if file_size > MAX_BYTES:
        raise WavRejected(
            "too_large",
            f"Capture is {file_size} bytes; the limit is 64 MiB ({MAX_BYTES} bytes)",
        )


def _parse_fmt(body: bytes) -> _Fmt:
    if len(body) < 16:
        raise WavRejected("not_riff_wave", "File is not a WAV file (fmt chunk is too short)")
    format_tag, channels, sample_rate, _byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", body, 0
    )
    subformat = None
    if format_tag == WAVE_FORMAT_EXTENSIBLE and len(body) >= 40:
        (cb_size,) = struct.unpack_from("<H", body, 16)
        if cb_size >= 22:
            subformat = bytes(body[24:40])
    return _Fmt(format_tag, channels, sample_rate, block_align, bits, subformat)


def _codec_text(fmt: _Fmt) -> str:
    if fmt.format_tag == WAVE_FORMAT_EXTENSIBLE:
        if fmt.subformat == PCM_SUBFORMAT_GUID:
            return "PCM (extensible)"
        return "WAVE_FORMAT_EXTENSIBLE with a non-PCM sub-format"
    if fmt.format_tag == WAVE_FORMAT_PCM:
        return "PCM"
    tag = f"format tag 0x{fmt.format_tag:04X}"
    name = _FORMAT_NAMES.get(fmt.format_tag)
    return f"{name} ({tag})" if name else tag


def _is_pcm(fmt: _Fmt) -> bool:
    if fmt.format_tag == WAVE_FORMAT_PCM:
        return True
    return fmt.format_tag == WAVE_FORMAT_EXTENSIBLE and fmt.subformat == PCM_SUBFORMAT_GUID


def _check_format(fmt: _Fmt) -> None:
    supported = (
        _is_pcm(fmt)
        and fmt.sample_rate == SUPPORTED_RATE
        and fmt.bits_per_sample == SUPPORTED_BITS
        and fmt.channels == SUPPORTED_CHANNELS
    )
    channel_word = "channel" if fmt.channels == 1 else "channels"
    detected = (
        f"Detected {_codec_text(fmt)}, {fmt.sample_rate} Hz, "
        f"{fmt.bits_per_sample}-bit, {fmt.channels} {channel_word}"
    )
    if not supported:
        raise WavRejected("unsupported_format", f"{detected}; {SUPPORTED_TEXT}")
    expected_align = fmt.channels * fmt.bits_per_sample // 8
    if fmt.block_align != expected_align:
        raise WavRejected(
            "unsupported_format",
            f"{detected} with block align {fmt.block_align} (expected {expected_align}); "
            f"{SUPPORTED_TEXT}",
        )


def _read_at(f: BinaryIO, offset: int, n: int) -> bytes:
    f.seek(offset)
    return f.read(n)


def _inspect(f: BinaryIO, file_size: int) -> WavInfo:
    _check_size(file_size)

    riff = _read_at(f, 0, 12)
    if len(riff) < 12 or riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise WavRejected("not_riff_wave", "File is not a WAV file")

    fmt: _Fmt | None = None
    data_bytes: int | None = None
    pos = 12
    while pos + 8 <= file_size and (fmt is None or data_bytes is None):
        chunk_header = _read_at(f, pos, 8)
        if len(chunk_header) < 8:
            break
        chunk_id = chunk_header[0:4]
        (chunk_size,) = struct.unpack_from("<I", chunk_header, 4)
        body_offset = pos + 8
        if chunk_id == b"fmt " and fmt is None:
            fmt = _parse_fmt(_read_at(f, body_offset, min(chunk_size, _FMT_READ_LIMIT)))
        elif chunk_id == b"data" and data_bytes is None:
            # A declared size past the end of the file (truncated or streamed
            # writes) is clamped to the bytes actually present.
            data_bytes = max(0, min(chunk_size, file_size - body_offset))
        pos = body_offset + chunk_size + (chunk_size & 1)

    if fmt is None:
        raise WavRejected("not_riff_wave", "File is not a WAV file (no fmt chunk)")
    _check_format(fmt)
    if not data_bytes:
        raise WavRejected("no_data", "Capture contains no audio data")

    duration_s = data_bytes / (fmt.sample_rate * fmt.block_align)
    if duration_s > MAX_SECONDS:
        raise WavRejected(
            "too_long",
            f"Capture is {duration_s:.1f} s long; the limit is 10 minutes ({MAX_SECONDS} s)",
        )
    return WavInfo(
        format_tag=fmt.format_tag,
        sample_rate=fmt.sample_rate,
        bits_per_sample=fmt.bits_per_sample,
        channels=fmt.channels,
        data_bytes=data_bytes,
        duration_s=duration_s,
    )


def inspect_wav_header(head: bytes, file_size: int) -> WavInfo:
    """Validate a capture from its leading bytes and its total size.

    ``head`` must contain the RIFF header and the fmt and data chunk headers.
    The audio samples themselves are not needed. Raises ``WavRejected``.
    """
    return _inspect(io.BytesIO(head), file_size)


def validate_capture(path: Path) -> WavInfo:
    """Validate a capture file on disk without modifying it. Raises ``WavRejected``."""
    path = Path(path)
    file_size = os.stat(path).st_size
    _check_size(file_size)
    with open(path, "rb") as f:
        return _inspect(f, file_size)
