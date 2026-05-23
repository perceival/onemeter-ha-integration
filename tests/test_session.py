"""End-to-end protocol-layer tests: build a session, build requests, feed
back synthetic responses, check events come out correctly."""
import struct

import pytest

from onemeter.protocol import commands
from onemeter.protocol.cipher import OneMeterCipher
from onemeter.protocol.crc import crc16_ccitt_false
from onemeter.protocol.framing import build_plain_frame
from onemeter.protocol.session import (
    BatteryReading,
    FrameErrorEvent,
    OneMeterSession,
    RejectionEvent,
    UnknownResponse,
)


def _encrypt_direct_response(cipher: OneMeterCipher, cmd: int, payload: bytes) -> bytes:
    """Build a direct single-frag response (payload at byte[1])."""
    if len(payload) > 13:
        raise ValueError("payload too long for single frame")
    body = bytes([cmd]) + payload + b"\x00" * (13 - len(payload))
    crc = crc16_ccitt_false(body)
    pt = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    return cipher.apply_constant_iv(pt)


def _encrypt_empty_ack(cipher: OneMeterCipher, cmd: int) -> bytes:
    """Build an empty-ACK frame (byte[1] = byte[2] = 0)."""
    body = bytes([cmd, 0, 0]) + b"\x00" * 11
    crc = crc16_ccitt_false(body)
    pt = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    return cipher.apply_constant_iv(pt)


def test_session_login_produces_three_frames(test_key, test_iv):
    s = OneMeterSession(test_key, test_iv)
    uuid = bytes(range(16))
    frames = s.build_login(uuid, unix_secs=0x11223344)
    assert len(frames) == 3
    assert all(len(f) == 16 for f in frames)

    # Decrypt each frame and check the cmd byte is the expected one.
    cipher = OneMeterCipher(test_key, test_iv)
    pts = [cipher.apply_constant_iv(f) for f in frames]
    assert pts[0][0] == commands.CMD_LOGIN
    assert pts[1][0] == commands.CMD_TIME_SYNC
    assert pts[2][0] == commands.CMD_START_READOUT


def test_session_build_stop_readout(test_key, test_iv):
    """Polite-close — cmd 0x23 with empty payload."""
    s = OneMeterSession(test_key, test_iv)
    cipher = OneMeterCipher(test_key, test_iv)
    frame = s.build_stop_readout()
    pt = cipher.apply_constant_iv(frame)
    assert pt[0] == commands.CMD_STOP_READOUT
    # body bytes 1..13 should all be zero (no payload)
    assert pt[1:14] == b"\x00" * 13


def test_session_keepalive_is_battery_request(test_key, test_iv):
    s = OneMeterSession(test_key, test_iv)
    frame = s.build_keepalive()
    cipher = OneMeterCipher(test_key, test_iv)
    pt = cipher.apply_constant_iv(frame)
    assert pt[0] == commands.CMD_BATTERY


def test_session_probe_forbidden_cmd_raises(test_key, test_iv):
    s = OneMeterSession(test_key, test_iv)
    with pytest.raises(commands.ForbiddenCommandError):
        s.build_probe(0x50)


def test_session_round_trip_battery_response(test_key, test_iv):
    """Synthesise a battery response and check the session decodes to BatteryReading."""
    s = OneMeterSession(test_key, test_iv)
    cipher = OneMeterCipher(test_key, test_iv)
    ct = _encrypt_direct_response(cipher, commands.CMD_BATTERY, b"\xe9\xe9\x00")
    event = s.feed_rx(ct)
    assert isinstance(event, BatteryReading)
    assert event.volts == pytest.approx(3.29, abs=0.01)


def test_session_login_ack_is_unknown_response(test_key, test_iv):
    """Login ACK has empty payload; session returns UnknownResponse so the
    coordinator can confirm receipt."""
    s = OneMeterSession(test_key, test_iv)
    cipher = OneMeterCipher(test_key, test_iv)
    ct = _encrypt_empty_ack(cipher, commands.CMD_LOGIN)
    event = s.feed_rx(ct)
    assert isinstance(event, UnknownResponse)
    assert event.cmd == commands.CMD_LOGIN
    assert event.payload == b""


def test_session_returns_rejection_event(test_key, test_iv):
    s = OneMeterSession(test_key, test_iv)
    cipher = OneMeterCipher(test_key, test_iv)
    body = bytes([0xFF, 0x01]) + b"\x00" * 12
    crc = crc16_ccitt_false(body)
    pt = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    ct = cipher.apply_constant_iv(pt)

    event = s.feed_rx(ct)
    assert isinstance(event, RejectionEvent)
    assert event.length == 1


def test_session_returns_frame_error_event(test_key, test_iv):
    s = OneMeterSession(test_key, test_iv)
    event = s.feed_rx(b"\x00" * 15)  # wrong length
    assert isinstance(event, FrameErrorEvent)


def test_session_reset_reassembler_clears_partial_state(test_key, test_iv):
    """A partial multi-frag state must be discarded by reset_reassembler()."""
    s = OneMeterSession(test_key, test_iv)
    cipher = OneMeterCipher(test_key, test_iv)

    # Send a multi-frag header for cmd 0x87 (28B coming).
    body = bytes([commands.CMD_IDENTITY, 0, 28]) + b"\x00" * 11
    crc = crc16_ccitt_false(body)
    pt = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    ct = cipher.apply_constant_iv(pt)
    assert s.feed_rx(ct) is None  # incomplete

    # ...then reset, then start a fresh battery response.
    s.reset_reassembler()
    ct2 = _encrypt_direct_response(cipher, commands.CMD_BATTERY, b"\xe9\xe9\x00")
    event = s.feed_rx(ct2)
    assert isinstance(event, BatteryReading)


def test_session_login_payload_contains_uuid_prefix_and_ts(test_key, test_iv):
    """Verify the login frame carries the right payload bytes."""
    s = OneMeterSession(test_key, test_iv)
    cipher = OneMeterCipher(test_key, test_iv)
    uuid = bytes(range(16))
    ts = 0x66666666
    frames = s.build_login(uuid, unix_secs=ts)
    pt = cipher.apply_constant_iv(frames[0])
    # body: [cmd][uuid[:9]][ts LE 4B][padding][CRC]
    assert pt[0] == commands.CMD_LOGIN
    assert pt[1:10] == uuid[:9]
    assert pt[10:14] == struct.pack("<I", ts)


def test_build_plain_frame_is_full_16_bytes():
    """Belt-and-braces — caller can build_plain_frame without session."""
    pt = build_plain_frame(commands.CMD_BATTERY, b"")
    assert len(pt) == 16
