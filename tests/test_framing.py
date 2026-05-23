"""Tests for frame build / encrypt / decrypt / reassemble.

Strategy: build known plaintext frames, encrypt with the test key+iv,
then feed back through the Reassembler. Exercises cipher, CRC, the
header-then-chained-data rule, and the three response shapes documented
in `framing.py`.
"""
import pytest

from onemeter.protocol import commands
from onemeter.protocol.cipher import OneMeterCipher
from onemeter.protocol.crc import crc16_ccitt_false
from onemeter.protocol.framing import (
    FRAME_LEN,
    FrameError,
    Reassembled,
    Reassembler,
    Rejected,
    build_plain_frame,
    check_crc,
    encrypt_request,
)


def _make_empty_ack(cmd: int) -> bytes:
    body = bytes([cmd, 0, 0]) + b"\x00" * 11
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _make_header_frame(cmd: int, total_len: int) -> bytes:
    body = bytes([cmd, 0, total_len]) + b"\x00" * 11
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _make_data_frame(cmd: int, chunk13: bytes) -> bytes:
    assert len(chunk13) == 13
    body = bytes([cmd]) + chunk13
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _make_direct_frame(cmd: int, payload_bytes: bytes) -> bytes:
    """Direct single-frag response — payload starts at byte[1]."""
    assert len(payload_bytes) <= 13
    body = bytes([cmd]) + payload_bytes + b"\x00" * (13 - len(payload_bytes))
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def test_build_plain_frame_length_and_crc():
    pt = build_plain_frame(commands.CMD_BATTERY, b"")
    assert len(pt) == FRAME_LEN
    assert pt[0] == commands.CMD_BATTERY
    assert check_crc(pt)


def test_build_plain_frame_rejects_forbidden_cmd():
    with pytest.raises(commands.ForbiddenCommandError):
        build_plain_frame(0x50, b"")


def test_build_plain_frame_payload_too_long():
    with pytest.raises(ValueError, match="payload too long"):
        build_plain_frame(commands.CMD_BATTERY, b"\x00" * 14)


def test_check_crc_rejects_wrong_length():
    assert not check_crc(b"\x00" * 15)
    assert not check_crc(b"\x00" * 17)


def test_check_crc_rejects_bad_crc():
    pt = build_plain_frame(commands.CMD_BATTERY, b"")
    bad = pt[:-1] + bytes([pt[-1] ^ 0xFF])
    assert not check_crc(bad)


def test_encrypt_request_roundtrip(test_key, test_iv):
    cipher = OneMeterCipher(test_key, test_iv)
    pt = build_plain_frame(commands.CMD_BATTERY, b"")
    ct = encrypt_request(cipher, commands.CMD_BATTERY, b"")
    assert cipher.apply_constant_iv(ct) == pt


def test_reassembler_empty_ack(test_key, test_iv):
    """ACK frame: byte[1] = byte[2] = 0."""
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)
    pt = _make_empty_ack(commands.CMD_LOGIN)
    ct = cipher.apply_constant_iv(pt)
    result = r.feed(ct)
    assert isinstance(result, Reassembled)
    assert result.cmd == commands.CMD_LOGIN
    assert result.payload == b""
    assert result.is_ack


def test_reassembler_direct_single_frag(test_key, test_iv):
    """Direct response: byte[1] != 0, payload starts at byte[1]."""
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)
    pt = _make_direct_frame(commands.CMD_BATTERY, b"\xe9\xe9\x00")
    ct = cipher.apply_constant_iv(pt)
    result = r.feed(ct)
    assert isinstance(result, Reassembled)
    assert result.cmd == commands.CMD_BATTERY
    # Returns full 13-byte body slice; caller takes [:3] for the 3 channels.
    assert len(result.payload) == 13
    assert result.payload[:3] == b"\xe9\xe9\x00"
    assert not result.is_ack


def test_reassembler_multifrag_header_plus_data(test_key, test_iv):
    """Multi-frag: header (constant IV) + data frames (chained IV)."""
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)

    # 28-byte payload split 13 + 13 + 2 across 3 data frames.
    payload = bytes(range(28))
    chunks = [
        payload[0:13],
        payload[13:26],
        payload[26:28] + b"\x00" * 11,
    ]

    hdr_pt = _make_header_frame(commands.CMD_COMM_STATS, total_len=28)
    d1_pt = _make_data_frame(commands.CMD_COMM_STATS, chunks[0])
    d2_pt = _make_data_frame(commands.CMD_COMM_STATS, chunks[1])
    d3_pt = _make_data_frame(commands.CMD_COMM_STATS, chunks[2])

    # Encryption: header with constant IV; data frames chain off the
    # previous frame's ciphertext.
    hdr_ct = cipher.apply_constant_iv(hdr_pt)
    d1_ct = cipher.apply_chained(d1_pt, hdr_ct)
    d2_ct = cipher.apply_chained(d2_pt, d1_ct)
    d3_ct = cipher.apply_chained(d3_pt, d2_ct)

    assert r.feed(hdr_ct) is None
    assert r.feed(d1_ct) is None
    assert r.feed(d2_ct) is None
    result = r.feed(d3_ct)
    assert isinstance(result, Reassembled)
    assert result.cmd == commands.CMD_COMM_STATS
    assert result.payload == payload  # trimmed to total_len
    assert not result.is_ack


def test_reassembler_multifrag_short(test_key, test_iv):
    """8-byte multi-frag payload — 1 header + 1 data frame."""
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)
    payload = bytes(range(8))
    hdr_pt = _make_header_frame(commands.CMD_LAST_OBIS, total_len=8)
    d1_pt = _make_data_frame(commands.CMD_LAST_OBIS, payload + b"\x00" * 5)
    hdr_ct = cipher.apply_constant_iv(hdr_pt)
    d1_ct = cipher.apply_chained(d1_pt, hdr_ct)
    assert r.feed(hdr_ct) is None
    result = r.feed(d1_ct)
    assert isinstance(result, Reassembled)
    assert result.payload == payload
    assert result.cmd == commands.CMD_LAST_OBIS


def test_reassembler_canned_rejection(test_key, test_iv):
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)
    body = bytes([0xFF, 0x01]) + b"\x00" * 12
    crc = crc16_ccitt_false(body)
    pt = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    ct = cipher.apply_constant_iv(pt)
    result = r.feed(ct)
    assert isinstance(result, Rejected)
    assert result.length == 1


def test_reassembler_bad_crc_raises_and_resets(test_key, test_iv):
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)

    pt = _make_direct_frame(commands.CMD_BATTERY, b"\xe9\xe9\x00")
    bad = bytearray(pt)
    bad[-1] ^= 0xFF
    ct = cipher.apply_constant_iv(bytes(bad))
    with pytest.raises(FrameError, match="CRC"):
        r.feed(ct)

    # After error, fresh frame should work.
    pt2 = _make_direct_frame(commands.CMD_BATTERY, b"\xee\xee\x00")
    ct2 = cipher.apply_constant_iv(pt2)
    result = r.feed(ct2)
    assert isinstance(result, Reassembled)


def test_reassembler_wrong_length_raises(test_key, test_iv):
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)
    with pytest.raises(FrameError, match="frame length"):
        r.feed(b"\x00" * 15)


def test_reassembler_cmd_change_mid_stream_raises(test_key, test_iv):
    cipher = OneMeterCipher(test_key, test_iv)
    r = Reassembler(cipher)

    hdr_pt = _make_header_frame(commands.CMD_IDENTITY, total_len=20)
    d1_pt = _make_data_frame(commands.CMD_BATTERY, b"\x00" * 13)  # wrong cmd
    hdr_ct = cipher.apply_constant_iv(hdr_pt)
    d1_ct = cipher.apply_chained(d1_pt, hdr_ct)

    assert r.feed(hdr_ct) is None
    with pytest.raises(FrameError, match="cmd byte changed"):
        r.feed(d1_ct)
