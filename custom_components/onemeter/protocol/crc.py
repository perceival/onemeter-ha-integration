"""CRC-16/CCITT-FALSE port from poc/onemeter_proto.py.

Used to checksum every 14-byte frame body. Init 0xFFFF, poly 0x1021, no XOR-out.
"""
from __future__ import annotations


def crc16_ccitt_false(data: bytes) -> int:
    """Compute CRC-16/CCITT-FALSE over `data`.

    Verified against the standard vector `crc("123456789") == 0x29B1`.
    """
    i2 = 0xFFFF
    for b in data:
        b &= 0xFF
        i3 = (((i2 << 8) | ((i2 >> 8) & 0xFF)) & 0xFFFF) ^ b
        i4 = i3 ^ (((i3 & 0xFF) >> 4) & 0xFFFF)
        i5 = i4 ^ (((i4 << 8) << 4) & 0xFFFF)
        i2 = i5 ^ ((((i5 & 0xFF) << 4) << 1) & 0xFFFF)
        i2 &= 0xFFFF
    return i2
