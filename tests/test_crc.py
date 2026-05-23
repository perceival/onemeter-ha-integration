from onemeter.protocol.crc import crc16_ccitt_false


def test_known_vector():
    """CRC-16/CCITT-FALSE("123456789") == 0x29B1 (standard test vector)."""
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_empty():
    assert crc16_ccitt_false(b"") == 0xFFFF


def test_canned_rejection_plaintext():
    """The device's canned auth-rejection plaintext has CRC 0x509D over
    its 14-byte body."""
    body = bytes.fromhex("ff01000000000000000000000000")
    assert crc16_ccitt_false(body) == 0x509D
