"""CRC-16/XMODEM vector tests for the BBC cassette CRC (Requirements 6.1, 6.2)."""

from taperescue.crc import crc16_xmodem


def _test_payload() -> bytes:
    """Test_Payload: payload[i] = (17*i + 11*i*i + 42) mod 256 for i in 0..255."""
    return bytes((17 * i + 11 * i * i + 42) % 256 for i in range(256))


def test_check_string_vector() -> None:
    assert crc16_xmodem(b"123456789") == 0x31C3


def test_payload_vector() -> None:
    payload = _test_payload()
    assert len(payload) == 256
    assert crc16_xmodem(payload) == 0xAC93
