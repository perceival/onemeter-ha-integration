import pytest

from onemeter.protocol import commands


def test_login_payload_shape():
    uuid = bytes(range(16))
    pl = commands.login_payload(uuid, unix_secs=0x11223344)
    assert pl == bytes(range(9)) + bytes([0x44, 0x33, 0x22, 0x11])
    assert len(pl) == 13


def test_login_payload_too_short_uuid():
    with pytest.raises(ValueError):
        commands.login_payload(b"\x00" * 8, unix_secs=0)


def test_time_sync_payload_is_le_u32():
    pl = commands.time_sync_payload(0x11223344)
    assert pl == bytes([0x44, 0x33, 0x22, 0x11])


def test_start_readout_payload():
    assert commands.start_readout_payload() == b"\x01"


def test_allowed_set_is_exactly_what_we_documented():
    assert commands.ALLOWED_DEV_COMMANDS == {
        0xAA, 0x13, 0x14, 0x19, 0x23, 0x18, 0x87, 0x36, 0x82, 0x21,
    }


def test_set_protocol_payload_shape():
    assert commands.set_protocol_payload(commands.PROTOCOL_IEC) == bytes([0x01, 0x00])
    assert commands.set_protocol_payload(commands.PROTOCOL_SML) == bytes([0x01, 0x01])
    assert commands.set_protocol_payload(commands.PROTOCOL_BLINK) == bytes([0x01, 0x02])
    assert commands.set_protocol_payload(commands.PROTOCOL_DLMS) == bytes([0x01, 0x03])


def test_set_protocol_payload_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        commands.set_protocol_payload(99)


@pytest.mark.parametrize("forbidden", [0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x59, 0xC1, 0xC2, 0xFF])
def test_forbidden_commands_raise(forbidden):
    with pytest.raises(commands.ForbiddenCommandError):
        commands.assert_allowed(forbidden)


@pytest.mark.parametrize("allowed", sorted([0xAA, 0x13, 0x14, 0x19, 0x23, 0x18, 0x87, 0x36, 0x82, 0x21]))
def test_allowed_commands_do_not_raise(allowed):
    commands.assert_allowed(allowed)
