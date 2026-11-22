"""Decoder adapter: runs the patched ScanTape decoder and parses its observations."""

from taperescue.decoder_adapter.run import (
    NO_BLOCKS_MESSAGE,
    capture_id_for,
    decode_capture,
    decoder_args,
)
from taperescue.decoder_adapter.schema import DecoderError, parse_observations

__all__ = [
    "NO_BLOCKS_MESSAGE",
    "DecoderError",
    "capture_id_for",
    "decode_capture",
    "decoder_args",
    "parse_observations",
]
