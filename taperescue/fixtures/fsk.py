"""FSK_Modulator: write BBC Micro 1200-baud cassette signals as WAV files.

The BBC Micro cassette interface records a ``0`` bit as one cycle of 1200 Hz
and a ``1`` bit as two cycles of 2400 Hz. Carrier tone is a run of ``1`` bits
(continuous 2400 Hz). Each byte is framed as a start bit ``0``, eight data
bits least significant first, and a stop bit ``1``.

Timing uses integer arithmetic only. At 44,100 Hz and 1200 baud a bit lasts
36.75 samples, so bit cell ``k`` (counted from the start of the stream) covers
samples ``floor(k * 147 / 4)`` up to ``floor((k + 1) * 147 / 4)``. Every cell is
36 or 37 samples. Sample values come from :data:`SINE_TABLE`, a fixed
256-entry int16 table at amplitude 16000, indexed by integer phase, and each
cell starts at phase 0 so frequency changes happen at a zero crossing. The
WAV header is written by hand with no metadata chunks. The same stream always
gives the same bytes (Requirement 4.9).

A stream is a sequence of segments:

* :class:`Carrier`: a count of 2400 Hz cycles (even, so it fills whole bit cells)
* :class:`Gap`: silence, a count of bit cells
* :class:`ByteFrames`: bytes, each written as a 10-bit frame

:func:`tape_stream` builds the stream for one tape file from the per-block
frame lists of :mod:`taperescue.fixtures.block_builder`, using the lead-in
timing in :data:`BBC_TIMING`.

This module must not import ``taperescue.decoder_adapter`` or run the decoder
(Requirement 4.8).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Sequence, Union

__all__ = [
    "AMPLITUDE",
    "BAUD",
    "BBC_TIMING",
    "CARRIER_HZ",
    "SAMPLE_RATE",
    "SINE_TABLE",
    "ByteFrames",
    "Carrier",
    "Gap",
    "Segment",
    "TapeTiming",
    "bit_start_sample",
    "modulate",
    "pcm_samples",
    "segment_start_samples",
    "tape_stream",
    "wav_header",
]

SAMPLE_RATE = 44_100
BAUD = 1_200
CARRIER_HZ = 2_400
AMPLITUDE = 16_000
BITS_PER_SAMPLE = 16
CHANNELS = 1

# 44100 / 1200 = 36.75 = 147 / 4 samples per bit.
_BIT_NUM = 147
_BIT_DEN = 4
assert SAMPLE_RATE * _BIT_DEN == BAUD * _BIT_NUM

_CYCLES_PER_ONE_BIT = CARRIER_HZ // BAUD   # 2
_PHASE_STEPS = 256

# round(16000 * sin(2 * pi * i / 256)) for i in 0..255, written out so the
# output never depends on the platform's libm.
SINE_TABLE: tuple[int, ...] = (
         0,    393,    785,   1177,   1568,   1959,   2348,   2735,
      3121,   3506,   3888,   4267,   4645,   5019,   5390,   5758,
      6123,   6484,   6841,   7194,   7542,   7886,   8226,   8560,
      8889,   9213,   9531,   9844,  10150,  10451,  10745,  11033,
     11314,  11588,  11855,  12115,  12368,  12614,  12851,  13081,
     13304,  13518,  13724,  13921,  14111,  14292,  14464,  14627,
     14782,  14928,  15065,  15192,  15311,  15420,  15521,  15611,
     15693,  15764,  15827,  15880,  15923,  15957,  15981,  15995,
     16000,  15995,  15981,  15957,  15923,  15880,  15827,  15764,
     15693,  15611,  15521,  15420,  15311,  15192,  15065,  14928,
     14782,  14627,  14464,  14292,  14111,  13921,  13724,  13518,
     13304,  13081,  12851,  12614,  12368,  12115,  11855,  11588,
     11314,  11033,  10745,  10451,  10150,   9844,   9531,   9213,
      8889,   8560,   8226,   7886,   7542,   7194,   6841,   6484,
      6123,   5758,   5390,   5019,   4645,   4267,   3888,   3506,
      3121,   2735,   2348,   1959,   1568,   1177,    785,    393,
         0,   -393,   -785,  -1177,  -1568,  -1959,  -2348,  -2735,
     -3121,  -3506,  -3888,  -4267,  -4645,  -5019,  -5390,  -5758,
     -6123,  -6484,  -6841,  -7194,  -7542,  -7886,  -8226,  -8560,
     -8889,  -9213,  -9531,  -9844, -10150, -10451, -10745, -11033,
    -11314, -11588, -11855, -12115, -12368, -12614, -12851, -13081,
    -13304, -13518, -13724, -13921, -14111, -14292, -14464, -14627,
    -14782, -14928, -15065, -15192, -15311, -15420, -15521, -15611,
    -15693, -15764, -15827, -15880, -15923, -15957, -15981, -15995,
    -16000, -15995, -15981, -15957, -15923, -15880, -15827, -15764,
    -15693, -15611, -15521, -15420, -15311, -15192, -15065, -14928,
    -14782, -14627, -14464, -14292, -14111, -13921, -13724, -13518,
    -13304, -13081, -12851, -12614, -12368, -12115, -11855, -11588,
    -11314, -11033, -10745, -10451, -10150,  -9844,  -9531,  -9213,
     -8889,  -8560,  -8226,  -7886,  -7542,  -7194,  -6841,  -6484,
     -6123,  -5758,  -5390,  -5019,  -4645,  -4267,  -3888,  -3506,
     -3121,  -2735,  -2348,  -1959,  -1568,  -1177,   -785,   -393,
)
assert len(SINE_TABLE) == _PHASE_STEPS


def _check_cycles(name: str, cycles: int) -> None:
    if not isinstance(cycles, int) or cycles < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {cycles!r}")
    if cycles % _CYCLES_PER_ONE_BIT:
        raise ValueError(f"{name} must be even (two 2400 Hz cycles per bit), got {cycles}")


def _cycles_for_seconds(seconds_tenths: int) -> int:
    """2400 Hz cycles in ``seconds_tenths / 10`` seconds (always even)."""
    return CARRIER_HZ * seconds_tenths // 10


@dataclass(frozen=True)
class TapeTiming:
    """Lead-in and trailer timing for one tape file, in integer units.

    Tone lengths are counts of 2400 Hz cycles and must be even (two cycles per
    bit cell). Silence lengths are counts of bit cells (1/1200 s each).
    """

    leading_gap_bits: int        # silence before the first tone
    first_prelude_cycles: int    # short tone before the dummy byte (first block only)
    dummy_byte: int              # byte written between prelude and lead tone
    first_lead_cycles: int       # tone after the dummy byte, before block 0
    other_lead_cycles: int       # tone before every later block
    trailer_cycles: int          # tone after the last block
    trailing_gap_bits: int       # silence at the end of the recording

    def __post_init__(self) -> None:
        for name in ("first_prelude_cycles", "first_lead_cycles",
                     "other_lead_cycles", "trailer_cycles"):
            _check_cycles(name, getattr(self, name))
        for name in ("leading_gap_bits", "trailing_gap_bits"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if not 0 <= self.dummy_byte <= 0xFF:
            raise ValueError(f"dummy_byte must be a byte, got {self.dummy_byte!r}")


# Lead-in constants, all in one place so they can be tuned against the decoder.
#
# | Field                | Value              | Source / decoder requirement                       |
# | -------------------- | ------------------ | -------------------------------------------------- |
# | leading_gap_bits     | 600 (0.5 s)        | Room before the first tone; no MOS equivalent       |
# | first_prelude_cycles | 4                  | AtomTapeUtils bbmTiming nominal prelude 4 cycles    |
# |                      |                    | (minimum 3), TapeProperties.h lines 104-109         |
# | dummy_byte           | 0xAA               | BBC MOS dummy byte between prelude and lead tone;   |
# |                      |                    | BlockDecoder.cpp comment lines 73-74                 |
# | first_lead_cycles    | 12240 (5.1 s)      | BBC MOS: 5.1 s of 2400 Hz leader before a file      |
# |                      |                    | (decoder minimum 2.0 s, nominal 5.1 s)              |
# | other_lead_cycles    | 2160 (0.9 s)       | BBC MOS: 0.9 s between blocks saved in one go       |
# |                      |                    | (decoder minimum 0.2 s, nominal 0.9 s)              |
# | trailer_cycles       | 4800 (2.0 s)       | Design value; decoder requires >= 0.5 s after the   |
# |                      |                    | last block (nominal 5.3 s)                          |
# | trailing_gap_bits    | 1200 (1.0 s)       | Silence after the trailer                           |
#
# BBC timing reference: 5.1 s of 2400 Hz leader before each file and 0.9 s
# between blocks, https://dsp.stackexchange.com/questions/72332 ; the decoder
# values are from AtomTapeUtils 98d1a8d1, shared/TapeProperties.h (bbmTiming).
BBC_TIMING = TapeTiming(
    leading_gap_bits=BAUD // 2,
    first_prelude_cycles=4,
    dummy_byte=0xAA,
    first_lead_cycles=_cycles_for_seconds(51),
    other_lead_cycles=_cycles_for_seconds(9),
    trailer_cycles=_cycles_for_seconds(20),
    trailing_gap_bits=BAUD,
)


@dataclass(frozen=True)
class Carrier:
    """Continuous 2400 Hz tone of ``cycles`` cycles (``cycles / 2`` bit cells)."""

    cycles: int

    def __post_init__(self) -> None:
        _check_cycles("carrier cycles", self.cycles)

    def bits(self) -> Iterable[int | None]:
        return (1,) * (self.cycles // _CYCLES_PER_ONE_BIT)


@dataclass(frozen=True)
class Gap:
    """Silence lasting ``cells`` bit cells."""

    cells: int

    def __post_init__(self) -> None:
        if not isinstance(self.cells, int) or self.cells < 0:
            raise ValueError(f"gap length must be a non-negative integer, got {self.cells!r}")

    def bits(self) -> Iterable[int | None]:
        return (None,) * self.cells


@dataclass(frozen=True)
class ByteFrames:
    """Bytes written as 10-bit frames: start 0, data bits LSB first, stop 1.

    ``values`` accepts ints or objects with a ``value`` attribute, such as
    :class:`taperescue.fixtures.block_builder.ByteFrame`.
    """

    values: tuple[int, ...]

    def __init__(self, values: Iterable) -> None:
        normalised = tuple(getattr(v, "value", v) for v in values)
        for v in normalised:
            if not isinstance(v, int) or not 0 <= v <= 0xFF:
                raise ValueError(f"byte frame value must be a byte, got {v!r}")
        object.__setattr__(self, "values", normalised)

    def bits(self) -> Iterable[int | None]:
        for v in self.values:
            yield 0
            for i in range(8):
                yield (v >> i) & 1
            yield 1


Segment = Union[Carrier, Gap, ByteFrames]


def bit_start_sample(k: int) -> int:
    """First sample of bit cell ``k``: ``floor(k * 147 / 4)``."""
    return k * _BIT_NUM // _BIT_DEN


def _bit_cells(segment: Segment) -> int:
    if isinstance(segment, Carrier):
        return segment.cycles // _CYCLES_PER_ONE_BIT
    if isinstance(segment, Gap):
        return segment.cells
    if isinstance(segment, ByteFrames):
        return 10 * len(segment.values)
    raise TypeError(f"not a segment: {segment!r}")


def segment_start_samples(stream: Sequence[Segment]) -> tuple[int, ...]:
    """Sample index where each segment starts, plus the total sample count last."""
    starts = []
    k = 0
    for segment in stream:
        starts.append(bit_start_sample(k))
        k += _bit_cells(segment)
    starts.append(bit_start_sample(k))
    return tuple(starts)


def _cell_waveform(bit: int | None, n: int) -> bytes:
    if bit is None:
        return bytes(2 * n)
    cycles = _CYCLES_PER_ONE_BIT if bit else 1
    samples = [SINE_TABLE[(_PHASE_STEPS * cycles * i // n) % _PHASE_STEPS] for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


# A cell is 36 or 37 samples; precompute the six possible waveforms.
_CELLS = {(bit, n): _cell_waveform(bit, n)
          for bit in (0, 1, None)
          for n in (_BIT_NUM // _BIT_DEN, _BIT_NUM // _BIT_DEN + 1)}


def pcm_samples(stream: Sequence[Segment]) -> bytes:
    """Return the little-endian int16 sample data for ``stream``."""
    chunks = []
    k = 0
    start = 0
    for segment in stream:
        if not isinstance(segment, (Carrier, Gap, ByteFrames)):
            raise TypeError(f"not a segment: {segment!r}")
        for bit in segment.bits():
            k += 1
            end = bit_start_sample(k)
            chunks.append(_CELLS[(bit, end - start)])
            start = end
    return b"".join(chunks)


def wav_header(data_bytes: int) -> bytes:
    """44-byte RIFF/WAVE header for 44.1 kHz, 16-bit, mono PCM."""
    block_align = CHANNELS * BITS_PER_SAMPLE // 8
    if not 0 <= data_bytes <= 0xFFFFFFFF - 36:
        raise ValueError(f"data size {data_bytes} does not fit in a WAV file")
    return (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, CHANNELS, SAMPLE_RATE,
                                SAMPLE_RATE * block_align, block_align, BITS_PER_SAMPLE)
        + b"data" + struct.pack("<I", data_bytes)
    )


def modulate(stream: Sequence[Segment]) -> bytes:
    """Return the complete WAV file bytes for ``stream``."""
    pcm = pcm_samples(stream)
    return wav_header(len(pcm)) + pcm


def tape_stream(blocks: Sequence[Sequence], timing: TapeTiming = BBC_TIMING) -> list[Segment]:
    """Build the segments for one tape file.

    ``blocks`` holds one frame list per block in tape order (as returned by
    :func:`taperescue.fixtures.block_builder.block_frames`, starting with the
    ``0x2A`` sync byte). The first block is preceded by the prelude tone, the
    dummy byte, and the long lead tone; later blocks by the short lead tone.
    The trailer tone and trailing silence follow the last block.
    """
    if not blocks:
        raise ValueError("a tape file needs at least one block")
    stream: list[Segment] = [Gap(timing.leading_gap_bits)]
    for index, frames in enumerate(blocks):
        if index == 0:
            stream += [Carrier(timing.first_prelude_cycles),
                       ByteFrames([timing.dummy_byte]),
                       Carrier(timing.first_lead_cycles)]
        else:
            stream.append(Carrier(timing.other_lead_cycles))
        stream.append(ByteFrames(frames))
    stream += [Carrier(timing.trailer_cycles), Gap(timing.trailing_gap_bits)]
    return stream
