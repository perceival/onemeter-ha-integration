"""Advertisement (passive broadcast) decoder tests."""
import json
import pathlib
import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from onemeter.protocol import advert

# Pinned vector: this exact advertisement decrypts to this clock and these
# records under conftest's TEST_KEY/TEST_IV. Regenerating it means re-deriving
# the wire layout by hand, which is the point — it fails if the layout drifts.
PINNED_ADVERT = bytes.fromhex("7c102324b4db38c6bb4862912f07f56e947e8b8e73901959")
PINNED_CLOCK = 1700000000
PINNED_RECORDS = ((0x0C, 1234567), (0x11, 42), (0x99, 0xDEADBEEF))
PINNED_QUARTER_HOUR = 89


def _build_plaintext(clock: int, records: tuple[tuple[int, int], ...]) -> bytes:
    return struct.pack("<I", clock) + b"".join(
        bytes([tag]) + struct.pack("<I", value) for tag, value in records
    )


# Synthetic captures shaped exactly like the real ones: a clock word plus three
# tagged 5-byte records, including the mirrored (total, tariff) pairs a OneMeter
# reports for the same register. None of the values comes from a real capture:
# the published repository should not carry a household's metered consumption. The
# genuine captured plaintexts live in tests/private/captures.json (gitignored)
# and are exercised by test_real_capture_plaintexts_when_available below.
CAPTURE_CLOCK = 1750000000
CAPTURE_A = _build_plaintext(
    CAPTURE_CLOCK, ((0x0C, 12345678), (0x11, 42), (0x16, 210000))
)
CAPTURE_B = _build_plaintext(
    CAPTURE_CLOCK, ((0x1B, 1120000), (0x0D, 12345678), (0x12, 42))
)


def _encrypt_advert(key: bytes, iv: bytes, plaintext: bytes, quarter_hour: int = 1) -> bytes:
    """Build a 24-byte data advert: ciphertext || tag || quarter-hour byte."""
    blob = AESCCM(key, tag_length=advert.ADVERT_TAG_LEN).encrypt(
        iv[: advert.ADVERT_NONCE_LEN], plaintext, iv[advert.ADVERT_NONCE_LEN:]
    )
    return blob + bytes([quarter_hour])


def test_decode_advertisement_pinned_vector(test_key, test_iv):
    result = advert.decode_advertisement(PINNED_ADVERT, test_key, test_iv)
    assert result is not None
    assert result.clock == PINNED_CLOCK
    assert [(r.tag, r.value) for r in result.records] == list(PINNED_RECORDS)
    assert result.quarter_hour == PINNED_QUARTER_HOUR


def test_quarter_hour_is_derived_from_the_authenticated_clock():
    """The wire byte is unauthenticated, so the value we report comes from the
    clock — and agrees with the byte the device actually sends."""
    assert advert.quarter_hour_of(PINNED_CLOCK) == PINNED_QUARTER_HOUR
    assert advert.quarter_hour_of(0) == 1
    assert advert.quarter_hour_of(900 * 95) == 96
    assert advert.quarter_hour_of(900 * 96) == 1


def test_decode_advertisement_minimal_advert_is_plaintext_clock():
    """The 9-byte advert carries the clock in the clear and needs no key."""
    clock = 1610612736
    payload = struct.pack("<I", clock) + b"\x00" * 5
    result = advert.decode_advertisement(payload)
    assert result is not None
    assert result.clock == clock
    assert result.records == ()
    assert result.quarter_hour is None


def test_decode_advertisement_data_advert_without_key_returns_none():
    """Without a key a data advert is unreadable, not half-decoded."""
    assert advert.decode_advertisement(PINNED_ADVERT) is None


def test_decode_advertisement_unknown_length_returns_none():
    assert advert.decode_advertisement(b"\x00" * 12, b"\x00" * 16, b"\x00" * 16) is None


def test_decrypt_advert_tampered_ciphertext_returns_none(test_key, test_iv):
    tampered = bytearray(PINNED_ADVERT)
    tampered[3] ^= 0x01
    assert advert.decrypt_advert(bytes(tampered), test_key, test_iv) is None


def test_decrypt_advert_tampered_tag_returns_none(test_key, test_iv):
    tampered = bytearray(PINNED_ADVERT)
    tampered[advert.ADVERT_MSG_LEN] ^= 0x01
    assert advert.decrypt_advert(bytes(tampered), test_key, test_iv) is None


def test_decrypt_advert_wrong_key_returns_none(test_key, test_iv):
    wrong_key = bytes(16)
    assert advert.decrypt_advert(PINNED_ADVERT, wrong_key, test_iv) is None


def test_decrypt_advert_short_payload_returns_none(test_key, test_iv):
    assert advert.decrypt_advert(PINNED_ADVERT[:20], test_key, test_iv) is None


def test_decrypt_advert_bad_key_length_raises(test_iv):
    """A wrong-size key is a configuration error, not remote input."""
    with pytest.raises(ValueError, match="key must be 16"):
        advert.decrypt_advert(PINNED_ADVERT, b"\x00" * 8, test_iv)


def test_decrypt_advert_bad_iv_length_raises(test_key):
    with pytest.raises(ValueError, match="iv must be 16"):
        advert.decrypt_advert(PINNED_ADVERT, test_key, b"\x00" * 8)


def test_decode_advertisement_round_trip(test_key, test_iv):
    """A freshly built advert decodes back to exactly what went into it, so
    the decoder is not merely agreeing with the pinned vector."""
    plaintext = _build_plaintext(1234567890, ((0x0C, 42), (0x1B, 7), (0x16, 0)))
    payload = _encrypt_advert(test_key, test_iv, plaintext, quarter_hour=7)
    result = advert.decode_advertisement(payload, test_key, test_iv)
    assert result is not None
    assert result.clock == 1234567890
    assert [(r.tag, r.value) for r in result.records] == [(0x0C, 42), (0x1B, 7), (0x16, 0)]
    assert result.quarter_hour == advert.quarter_hour_of(1234567890)
    assert result.raw == payload


def test_wire_quarter_hour_byte_is_ignored(test_key, test_iv):
    """The trailing byte sits outside the CCM message, so an attacker-chosen
    copy of it must not change what we report — the value is derived from the
    authenticated clock instead."""
    plaintext = _build_plaintext(1234567890, ((0x0C, 42), (0x1B, 7), (0x16, 0)))
    payload = bytearray(_encrypt_advert(test_key, test_iv, plaintext, quarter_hour=7))
    payload[-1] = 200   # unauthenticated: anyone can rewrite this on any advert
    result = advert.decode_advertisement(bytes(payload), test_key, test_iv)
    assert result is not None
    assert result.quarter_hour == advert.quarter_hour_of(1234567890)


def test_parse_advertisement_fixture_plaintext():
    """Clock plus three tagged records, in the shape a real capture has."""
    result = advert.parse_advertisement(CAPTURE_A)
    assert result is not None
    assert result.clock == CAPTURE_CLOCK
    assert [(r.tag, r.value) for r in result.records] == [
        (0x0C, 12345678),  # 0.1.8.0 energy import, raw units of 10 Wh
        (0x11, 42),        # 0.2.8.0 energy export
        (0x16, 210000),    # 0.3.8.0 tariff 2
    ]


def test_parse_advertisement_mirrored_pair_carries_same_import():
    """The same register shows up under two tags (the device reports a
    (total, tariff) pair, equal on a single-tariff meter) — so a tag is a
    firmware identifier, not a unique register slot, and both map to one
    OBIS code."""
    result = advert.parse_advertisement(CAPTURE_B)
    assert result is not None
    assert [(r.tag, r.value) for r in result.records] == [
        (0x1B, 1120000),   # 0.4.8.0 tariff 3
        (0x0D, 12345678),  # 0.1.8.0 again, under the paired tag
        (0x12, 42),        # 0.2.8.0 again
    ]
    paired = result.record(0x0D)
    assert paired is not None
    assert paired.obis == bytes([0, 1, 8, 0])


def test_parse_advertisement_short_plaintext_returns_none():
    assert advert.parse_advertisement(CAPTURE_A[:18]) is None


def test_obis_values_maps_identified_tags():
    result = advert.parse_advertisement(CAPTURE_A)
    assert result is not None
    assert result.obis_values() == {
        bytes([0, 1, 8, 0]): 12345678,
        bytes([0, 2, 8, 0]): 42,
        bytes([0, 3, 8, 0]): 210000,
    }


_PRIVATE_CAPTURES = pathlib.Path(__file__).resolve().parent / "private" / "captures.json"


def test_real_capture_plaintexts_when_available():
    """Cross-check against the genuine captured plaintexts where they exist.

    Those bytes are a real household's meter readings, so they are deliberately
    not part of the published fixtures — but they are the strongest regression
    data in this project (they came off a device on a live meter), so this test
    runs them whenever ``tests/private/captures.json`` is present (see that
    directory's .gitignore) and skips elsewhere. Only *relationships* are
    asserted, never the captured numbers, so nothing private is duplicated into
    this file.
    """
    if not _PRIVATE_CAPTURES.exists():
        pytest.skip("no tests/private/captures.json on this machine")
    data = json.loads(_PRIVATE_CAPTURES.read_text())
    cap_a = advert.parse_advertisement(bytes.fromhex(data["advert_a"]))
    cap_b = advert.parse_advertisement(bytes.fromhex(data["advert_b"]))
    assert cap_a is not None and cap_b is not None
    assert cap_a.clock == data["clock"]

    values = cap_a.obis_values()
    assert bytes([0, 1, 8, 0]) in values and bytes([0, 2, 8, 0]) in values
    assert values[bytes([0, 2, 8, 0])] < values[bytes([0, 1, 8, 0])]  # export < import
    paired = cap_b.record(0x0D)  # the mirrored tag of 0x0C
    assert paired is not None
    assert paired.value == values[bytes([0, 1, 8, 0])]
    assert paired.obis == bytes([0, 1, 8, 0])


def test_obis_values_drops_sentinel_and_unmapped_tags():
    """Unidentified tags and the device's "no value" sentinel are omitted
    rather than guessed at."""
    plaintext = _build_plaintext(
        CAPTURE_CLOCK,
        ((0x0C, 5), (0x99, 7), (0x1B, advert.SENTINEL_NO_VALUE)),
    )
    result = advert.parse_advertisement(plaintext)
    assert result is not None
    assert result.obis_values() == {bytes([0, 1, 8, 0]): 5}


def test_record_returns_none_for_absent_tag():
    result = advert.parse_advertisement(CAPTURE_A)
    assert result is not None
    assert result.record(0x00) is None


def test_mirrored_tag_pairs_share_one_obis_code():
    assert advert.ADVERT_TAG_OBIS[0x0C] == advert.ADVERT_TAG_OBIS[0x0D]
    assert advert.ADVERT_TAG_OBIS[0x11] == advert.ADVERT_TAG_OBIS[0x12]


def test_clock_and_unidentified_tags_stay_unmapped():
    """0x4D tracks the device clock, but 255.1.1.4 is documented in obis_map as
    the *last meter read* and exposed as a TIMESTAMP entity — mapping it there
    would write a live clock into that entity, so it stays raw. 0x4F/0x50/0x5D
    have no matched register at all."""
    for tag in (0x4D, 0x4F, 0x50, 0x5D):
        assert tag not in advert.ADVERT_TAG_OBIS
