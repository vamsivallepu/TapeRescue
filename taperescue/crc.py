"""CRC-16/XMODEM as used by the BBC Micro cassette format.

Polynomial 0x1021, initial value 0x0000, no input or output reflection,
final XOR 0x0000. The BBC format stores the CRC high byte first. The header
CRC covers the filename through the next-file address; the data CRC covers
the payload only.
"""

from __future__ import annotations

__all__ = ["POLY", "crc16_xmodem"]

POLY = 0x1021


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def crc16_xmodem(data: bytes | bytearray | memoryview, init: int = 0x0000) -> int:
    """Return the CRC-16/XMODEM of ``data`` starting from ``init``.

    Passing a previous result as ``init`` continues the calculation, so
    ``crc16_xmodem(b, crc16_xmodem(a)) == crc16_xmodem(a + b)``.
    """
    if not 0 <= init <= 0xFFFF:
        raise ValueError(f"init must be in 0..0xFFFF, got {init!r}")
    crc = init
    table = _TABLE
    for byte in bytes(data):
        crc = ((crc << 8) & 0xFFFF) ^ table[(crc >> 8) ^ byte]
    return crc
