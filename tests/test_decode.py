"""Decoder unit tests."""
import pytest
from onemeter.protocol import decode


def test_parse_battery_at_3_29v():
    """Three-channel battery readout `e9 e9 00` → ~3.29 V."""
    v = decode.parse_battery(b"\xe9\xe9\x00")
    assert v is not None
    assert v == pytest.approx(3.29, abs=0.01)


def test_parse_battery_empty():
    assert decode.parse_battery(b"") is None


def test_parse_identity():
    """Identity blob — serial is u32 LE of the first 4 bytes.

    Bytes `65 bd 03 4f` interpreted little-endian = 0x4F03BD65.
    """
    blob = bytes.fromhex("65bd034f000000000709e50136a06863")
    ident = decode.parse_identity(blob)
    assert ident is not None
    assert ident.serial == 0x4F03BD65
    assert ident.mac == bytes.fromhex("e50136a06863")
    assert ident.status == bytes.fromhex("0709")


def test_parse_identity_short_payload():
    assert decode.parse_identity(b"\x00" * 15) is None


def test_format_mac():
    assert (
        decode.format_mac(bytes.fromhex("e50136a06863"))
        == "E5:01:36:A0:68:63"
    )


def test_format_mac_wrong_length():
    with pytest.raises(ValueError):
        decode.format_mac(b"\x00" * 5)


def test_parse_last_obis_entry():
    """8-byte entry layout: 4B OBIS + 4B u32 LE value."""
    raw = bytes.fromhex("ff01010a") + bytes.fromhex("ffffffff")
    entries = decode.parse_last_obis(raw)
    assert len(entries) == 1
    assert entries[0].obis == bytes.fromhex("ff01010a")
    assert entries[0].value == 0xFFFFFFFF


def test_parse_last_obis_trailing_partial_dropped():
    raw = bytes(8) + bytes(3)  # one good entry + 3-byte trailing junk
    entries = decode.parse_last_obis(raw)
    assert len(entries) == 1


def test_parse_last_obis_empty():
    assert decode.parse_last_obis(b"") == []


def test_parse_last_obis_real_device_capture():
    """Full cmd 0x21 payload captured live from a device with a real
    meter attached (Apator NORAX 3), 2026-09-17 — the first time this
    path was exercised against genuine accumulated data rather than
    the all-0xFF "no meter" sentinel devices without a meter return.

    Cross-checks below aren't just decode sanity: they're evidence the
    bytes are real energy-register data, not noise.
    """
    raw = bytes.fromhex(
        "000009010085000000000902193500000001080082333d000001080182333d"
        "0000010802000000000002080072000000000208017200000000020802000000"
        "0000030800153c030000030801153c0300000308020000000000040800e60f11"
        "0000040801e60f11000004080200000000000f0800f5333d00000f0801f5333d"
        "00000f080200000000000f080300000000000f080400000000014301007dac8e"
        "05000f070031000000ff010104f896ab6aff01010663030000ff010107b10000"
        "00ff01010af896ab6aff01010b4410ce00ff01010e3914ced3ff0101110002ff"
        "7fff0101133b000000"
    )
    entries = decode.parse_last_obis(raw)
    assert len(entries) == 29
    by_obis = {e.obis: e.value for e in entries}

    # 1.8.0 (active energy import) — no sentinel, plausible household kWh
    # register (raw units of 10 Wh, per obis_map.py's 0.01 scale factor).
    e_1_8_0 = by_obis[bytes([0, 1, 8, 0])]
    e_2_8_0 = by_obis[bytes([0, 2, 8, 0])]
    e_15_8_0 = by_obis[bytes([0, 15, 8, 0])]
    assert e_1_8_0 == 4010882
    # 15.8.0 (sum active energy) is import + export, within rounding.
    assert abs(e_15_8_0 - (e_1_8_0 + e_2_8_0)) <= 1

    # 255.1.1.4 — device's "last meter read" timestamp (unix seconds).
    # Captured live on 2026-09-17; decodes to that same day.
    import datetime

    ts = by_obis[bytes([0xFF, 1, 1, 4])]
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
    assert dt.date() == datetime.date(2026, 9, 17)

    # None of these are the 0xFFFFFFFF "no value" sentinel.
    assert all(v != 0xFFFFFFFF for v in by_obis.values())


def test_parse_comm_stats_empty_payload_is_none():
    assert decode.parse_comm_stats(b"") is None


def test_parse_comm_stats_truncated_is_none():
    assert decode.parse_comm_stats(b"\x00" * 27) is None


def test_parse_comm_stats_all_zero():
    """Device that's never talked to a meter — every counter at 0."""
    raw = b"\x00" * 28
    stats = decode.parse_comm_stats(raw)
    assert stats is not None
    assert stats.succeeded_total == 0
    assert stats.failed_total == 0
    assert stats.day_cycles_completed == 0
    assert stats.raw == raw


def test_parse_auto_detect_basic():
    payload = bytes([0x02, 0x01, 0x05, 0x03])
    r = decode.parse_auto_detect(payload)
    assert r is not None
    assert r.status == 2
    assert r.protocol == 1
    assert r.baud_index == 5
    assert r.extras == 3
    assert "SML" in r.summary()


def test_parse_auto_detect_too_short():
    assert decode.parse_auto_detect(b"\x00\x01\x02") is None


def test_parse_device_time_basic():
    payload = bytes([0x44, 0x33, 0x22, 0x11]) + b"\x00" * 9
    r = decode.parse_device_time(payload)
    assert r is not None
    assert r.clock == 0x11223344
    assert r.raw == bytes([0x44, 0x33, 0x22, 0x11])


def test_parse_device_time_too_short():
    assert decode.parse_device_time(b"\x00\x01\x02") is None


def test_parse_block_header_basic():
    import struct
    payload = bytes([0x01]) + struct.pack("<I", 0x6a10da42) + b"\x00" * 7
    h = decode.parse_block_header(payload)
    assert h is not None
    assert h.data_type == 1
    assert h.timestamp == 0x6a10da42


def test_parse_block_header_data_type_zero_returns_none():
    payload = b"\x00" + b"\x00" * 11
    assert decode.parse_block_header(payload) is None


def test_parse_block_header_too_short():
    assert decode.parse_block_header(b"\x01\x00") is None


def test_parse_data_record_with_value():
    import struct
    payload = struct.pack("<II", 42, 0xDEADBEEF) + bytes([0x10, 0x20, 0x30, 0x40])
    r = decode.parse_data_record(payload)
    assert r is not None
    assert r.field0 == 42
    assert r.sentinel == 0xDEADBEEF
    assert r.raw_value == bytes([0x10, 0x20, 0x30, 0x40])
    assert r.value_u32 == 0x40302010
    assert r.has_value


def test_parse_data_record_no_value_sentinel():
    import struct
    payload = struct.pack("<II", 0, decode.SENTINEL_NO_VALUE) + b"\xff\xff\xff\xff"
    r = decode.parse_data_record(payload)
    assert r is not None
    assert r.sentinel == decode.SENTINEL_NO_VALUE
    assert not r.has_value


def test_parse_data_record_too_short():
    assert decode.parse_data_record(b"\x00" * 11) is None


def test_parse_comm_stats_realistic_payload():
    """Synthesise a payload with distinct counters in each field."""
    import struct
    # u32 LE succeeded_total = 0x12345678
    # u16 LE rest, increasing: 1, 2, 3, ...
    raw = struct.pack("<IHHHHHHHHHHHH",
                      0x12345678,    # succeeded_total
                      1,             # failed_total
                      2,             # day_cycles_completed
                      3,             # failed_on_id
                      4,             # failed_on_data
                      5,             # tx_power_on_fail
                      6,             # succeeded_subcnt
                      7,             # failed_on_id_subcnt
                      8,             # failed_on_data_subcnt
                      9,             # succeeded_on_demand
                      10,            # failed_on_demand
                      11,            # hardware_readouts
                      12)            # software_readouts
    stats = decode.parse_comm_stats(raw)
    assert stats is not None
    assert stats.succeeded_total == 0x12345678
    assert stats.failed_total == 1
    assert stats.day_cycles_completed == 2
    assert stats.failed_on_id == 3
    assert stats.failed_on_data == 4
    assert stats.tx_power_on_fail == 5
    assert stats.succeeded_subcnt == 6
    assert stats.failed_on_id_subcnt == 7
    assert stats.failed_on_data_subcnt == 8
    assert stats.succeeded_on_demand == 9
    assert stats.failed_on_demand == 10
    assert stats.hardware_readouts == 11
    assert stats.software_readouts == 12
