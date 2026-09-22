"""Passive advertisement decoding (the broadcast channel).

The device emits two advertisement shapes under the same manufacturer ID
(0xFFFF). Both arrive here as the manufacturer-data *value*, i.e. the bytes
following the company ID — exactly what HA's
`BluetoothServiceInfo.manufacturer_data[0xFFFF]` and bleak's
`manufacturer_data[0xFFFF]` hand over.

9-byte minimal advert — plaintext, no key required::

    [0:4]   device clock, u32 LE (unix seconds)
    [4:9]   reserved

24-byte data advert::

    [0:19]  AES-CCM ciphertext
    [19:23] AES-CCM authentication tag (4 bytes)
    [23]    quarter-hour counter, plaintext and *outside* the CCM message

Byte 23 sits outside the authenticated message, so it is ignored on decode:
`Advertisement.quarter_hour` is derived from the authenticated clock instead.
The device computes that wire byte the same way (verified across three
devices), so nothing is lost by not trusting an attacker-mutable copy of it.

The 19-byte CCM message decrypts to the clock plus three fixed-size records::

    [0:4]   device clock, u32 LE
    [4:19]  (tag u8 + value u32 LE) x 3

The cipher is RFC 3610 CCM with a 4-byte tag, a 13-byte nonce taken from the
start of the device's slot-2 IV, and associated data = the last 3 bytes of
that same IV. The key is the device's *slot-2* key, which is NOT the mobKey
used by the GATT session protocol — see `tools/extract_credentials.py`.

Only the records in `ADVERT_TAG_OBIS` have been identified so far; the rest
are exposed raw so they can be mapped later. Coverage is deliberately partial:
battery, comm stats, identity, FS params and most of the cached OBIS set are
not broadcast and still require an active session.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from .decode import SENTINEL_NO_VALUE

ADVERT_MINIMAL_LEN = 9
ADVERT_DATA_LEN = 24
ADVERT_MSG_LEN = 19
ADVERT_TAG_LEN = 4
ADVERT_NONCE_LEN = 13
ADVERT_RECORD_LEN = 5
ADVERT_RECORD_COUNT = 3
ADVERT_QUARTER_HOUR_DIVISOR = 900   # 15 minutes
ADVERT_QUARTER_HOUR_MODULO = 96     # 24 h / 15 min

# Firmware register tags identified against the stored cmd 0x21 datagram
# (tests/test_decode.py) captured from a meter with real accumulated data.
# 0x0c/0x0d and 0x11/0x12 are mirrored pairs — the device reports both
# members of a (total, tariff) pair with the same value on a single-tariff
# meter, so both tags map to the same OBIS code.
ADVERT_TAG_OBIS: dict[int, bytes] = {
    0x0C: bytes([0, 1, 8, 0]),      # 0.1.8.0  energy import total
    0x0D: bytes([0, 1, 8, 0]),      # 0.1.8.0  mirrored pair member
    0x11: bytes([0, 2, 8, 0]),      # 0.2.8.0  energy export total
    0x12: bytes([0, 2, 8, 0]),      # 0.2.8.0  mirrored pair member
    0x16: bytes([0, 3, 8, 0]),      # 0.3.8.0  reactive energy consumed
    0x1B: bytes([0, 4, 8, 0]),      # 0.4.8.0  reactive energy returned
    0x56: bytes([0xFF, 1, 1, 11]),  # 255.1.1.11
    0x57: bytes([0xFF, 1, 1, 14]),  # 255.1.1.14
}

# Broadcast tags seen on real devices with no register matched yet: 0x4F, 0x50,
# 0x5D. Deliberately absent from ADVERT_TAG_OBIS:
#   0x4D — tracks the device clock (advances ~1/s), but obis_map.py documents
#          255.1.1.4 as the *last meter read* and exposes it as a TIMESTAMP
#          entity, so mapping this tag there would write a live clock into that
#          entity. The authenticated clock is already exposed as
#          `Advertisement.clock`; leave the tag unmapped until the two are
#          reconciled.


@dataclass(frozen=True)
class AdvertRecord:
    """One tagged register reading carried by a data advertisement."""

    tag: int    # firmware register tag, see ADVERT_TAG_OBIS
    value: int  # raw register value, u32 LE

    @property
    def obis(self) -> bytes | None:
        """OBIS code this tag has been identified as, if any (else None)."""
        return ADVERT_TAG_OBIS.get(self.tag)


@dataclass(frozen=True)
class Advertisement:
    """A decoded advertisement.

    The minimal advert yields only `clock`; a data advert additionally fills
    `records` and `quarter_hour`.
    """

    clock: int                               # device clock, unix seconds
    records: tuple[AdvertRecord, ...] = ()   # empty for the minimal advert
    quarter_hour: int | None = None          # 1-based, wraps at 96
    raw: bytes = b""

    def record(self, tag: int) -> AdvertRecord | None:
        """The record carrying `tag`, or None if this advert lacks it."""
        for rec in self.records:
            if rec.tag == tag:
                return rec
        return None

    def obis_values(self) -> dict[bytes, int]:
        """Identified records as {obis code: raw value}, sentinels dropped.

        Records whose tag is not yet mapped, and records carrying the
        device's "no value" sentinel, are omitted rather than guessed at.
        """
        out: dict[bytes, int] = {}
        for rec in self.records:
            obis = rec.obis
            if obis is not None and rec.value != SENTINEL_NO_VALUE:
                out[obis] = rec.value
        return out


def quarter_hour_of(clock: int) -> int:
    """The 1-based quarter-hour index the device stamps on its adverts.

    Derived from the clock rather than read off the wire, because the wire byte
    sits outside the CCM message and is therefore unauthenticated.
    """
    return (clock // ADVERT_QUARTER_HOUR_DIVISOR) % ADVERT_QUARTER_HOUR_MODULO + 1


def parse_minimal_advertisement(payload: bytes) -> Advertisement | None:
    """Decode the 9-byte plaintext advert (clock only, never encrypted)."""
    if len(payload) != ADVERT_MINIMAL_LEN:
        return None
    return Advertisement(clock=struct.unpack_from("<I", payload, 0)[0], raw=payload)


def decrypt_advert(payload: bytes, key: bytes, iv: bytes) -> bytes | None:
    """Decrypt a 24-byte advert's CCM message, or None if it fails to verify.

    A wrong key and a corrupted payload are indistinguishable here by design:
    both mean "this advertisement is not ours to read", which is not an error
    condition. Key/IV of the wrong length *are* errors (a config mistake, not
    remote input), so they raise.
    """
    if len(payload) != ADVERT_DATA_LEN:
        return None
    if len(key) != 16:
        raise ValueError(f"key must be 16 bytes, got {len(key)}")
    if len(iv) != 16:
        raise ValueError(f"iv must be 16 bytes, got {len(iv)}")
    ciphertext = payload[:ADVERT_MSG_LEN]
    tag = payload[ADVERT_MSG_LEN:ADVERT_MSG_LEN + ADVERT_TAG_LEN]
    ccm = AESCCM(key, tag_length=ADVERT_TAG_LEN)
    try:
        return ccm.decrypt(iv[:ADVERT_NONCE_LEN], ciphertext + tag, iv[ADVERT_NONCE_LEN:])
    except InvalidTag:
        return None


def parse_advertisement(
    plaintext: bytes,
    *,
    quarter_hour: int | None = None,
    raw: bytes = b"",
) -> Advertisement | None:
    """Split an already-decrypted 19-byte CCM message into clock + records.

    `quarter_hour` defaults to the value derived from the authenticated clock;
    pass it explicitly only to override that.
    """
    if len(plaintext) != ADVERT_MSG_LEN:
        return None
    clock = struct.unpack_from("<I", plaintext, 0)[0]
    records = tuple(
        AdvertRecord(
            tag=plaintext[4 + i * ADVERT_RECORD_LEN],
            value=struct.unpack_from("<I", plaintext, 5 + i * ADVERT_RECORD_LEN)[0],
        )
        for i in range(ADVERT_RECORD_COUNT)
    )
    return Advertisement(
        clock=clock,
        records=records,
        quarter_hour=quarter_hour if quarter_hour is not None else quarter_hour_of(clock),
        raw=raw,
    )


def decode_advertisement(
    payload: bytes,
    key: bytes | None = None,
    iv: bytes | None = None,
) -> Advertisement | None:
    """Decode either advertisement shape, returning None if it can't be read.

    Without a key only the 9-byte minimal advert decodes (it is plaintext);
    a data advert then returns None rather than half-decoded garbage.
    """
    if len(payload) == ADVERT_MINIMAL_LEN:
        return parse_minimal_advertisement(payload)
    if key is None or iv is None:
        return None
    plaintext = decrypt_advert(payload, key, iv)
    if plaintext is None:
        return None
    return parse_advertisement(plaintext, raw=payload)
