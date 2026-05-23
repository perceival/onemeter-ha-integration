"""Decoders for the read-probe response payloads.

These take the *reassembled* payload bytes (header byte + length byte
stripped) and produce structured data. Decoders are defensive: malformed
or short payloads return ``None`` rather than raising, so the integration
can degrade gracefully on unexpected device states.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# Battery voltage scale factor: raw ADC byte × this = volts. The ADC
# uses the nRF51's VDD/3 prescaler against the 1.2 V bandgap, giving
# 0.01406-ish V per LSB; the precise factor is from the firmware's
# fixed-point reciprocal.
BATTERY_SCALE_V = 0.014117788236705895


def parse_battery(payload: bytes) -> float | None:
    """Decode cmd 0x18 (battery) payload to volts.

    Payload is 3 bytes, one per ADC channel. We return the first channel's
    voltage; channels 2-3 are duplicates / unused on this hardware.
    """
    if len(payload) < 1:
        return None
    return payload[0] * BATTERY_SCALE_V


@dataclass(frozen=True)
class Identity:
    """Decoded cmd 0x87 device identity blob (16 bytes)."""

    serial: int          # u32 big-endian — used as the device's integer serial number
    status: bytes        # 2 status bytes (bytes 8..9)
    mac: bytes           # 6-byte BLE MAC (bytes 10..15)
    raw: bytes


def parse_identity(payload: bytes) -> Identity | None:
    """Decode cmd 0x87 payload."""
    if len(payload) < 16:
        return None
    serial = struct.unpack_from("<I", payload, 0)[0]
    # bytes 4..7 are zero on the live device — kept in raw for diagnostics.
    status = bytes(payload[8:10])
    mac = bytes(payload[10:16])
    return Identity(serial=serial, status=status, mac=mac, raw=bytes(payload[:16]))


def format_mac(mac: bytes) -> str:
    """Render a 6-byte MAC as colon-separated upper-case hex."""
    if len(mac) != 6:
        raise ValueError(f"mac must be 6 bytes, got {len(mac)}")
    return ":".join(f"{b:02X}" for b in mac)


@dataclass(frozen=True)
class CommStats:
    """Decoded cmd 0x36 communication-statistics blob (28 bytes).

    All counters are little-endian. ``succeeded_total`` is the key
    signal that the device has *ever* successfully read the meter —
    useful for diagnostics and for gating features that only make
    sense once meter communication is confirmed.
    """

    succeeded_total: int       # u32 — total successful meter reads
    failed_total: int          # u16 — total failed
    day_cycles_completed: int  # u16
    failed_on_id: int          # u16 — failed during ID frame
    failed_on_data: int        # u16 — failed during data frame
    tx_power_on_fail: int      # u16
    succeeded_subcnt: int      # u16
    failed_on_id_subcnt: int   # u16
    failed_on_data_subcnt: int # u16
    succeeded_on_demand: int   # u16
    failed_on_demand: int      # u16
    hardware_readouts: int     # u16
    software_readouts: int     # u16
    raw: bytes


def parse_comm_stats(payload: bytes) -> CommStats | None:
    """Decode cmd 0x36 payload."""
    if len(payload) < 28:
        # Empty payload (e.g. device with no comm history) or truncated:
        # bail. Caller treats None as "no stats available yet".
        return None
    s = struct.unpack_from("<IHHHHHHHHHHHH", payload, 0)
    return CommStats(
        succeeded_total=s[0],
        failed_total=s[1],
        day_cycles_completed=s[2],
        failed_on_id=s[3],
        failed_on_data=s[4],
        tx_power_on_fail=s[5],
        succeeded_subcnt=s[6],
        failed_on_id_subcnt=s[7],
        failed_on_data_subcnt=s[8],
        succeeded_on_demand=s[9],
        failed_on_demand=s[10],
        hardware_readouts=s[11],
        software_readouts=s[12],
        raw=bytes(payload[:28]),
    )


@dataclass(frozen=True)
class FSParams:
    """Decoded cmd 0x82 filesystem-params blob."""

    raw: bytes


def parse_fs_params(payload: bytes) -> FSParams | None:
    if len(payload) == 0:
        return None
    return FSParams(raw=bytes(payload))


@dataclass(frozen=True)
class ObisEntry:
    """One (obis-code, value) entry from cmd 0x21's cached readout list."""

    obis: bytes          # 4-byte OBIS code
    value: int           # u32 little-endian (sentinel 0xFFFFFFFF = "no value")
    raw: bytes


def parse_last_obis(payload: bytes) -> list[ObisEntry]:
    """Decode cmd 0x21 payload into a list of cached OBIS entries.

    Entries are 8 bytes each: 4 byte OBIS code + 4 byte little-endian value.
    A trailing partial entry (payload length not divisible by 8) is dropped.
    """
    entries: list[ObisEntry] = []
    for i in range(0, len(payload) - (len(payload) % 8), 8):
        obis = bytes(payload[i : i + 4])
        value = struct.unpack_from("<I", payload, i + 4)[0]
        entries.append(ObisEntry(obis=obis, value=value, raw=bytes(payload[i : i + 8])))
    return entries


# --- Live-stream frames (cmd 0x25 + cmd 0x20) -------------------------------
#
#   cmd 0x25 = block header  : [dataType:u8][unix_ts:u32 LE] + filler
#   cmd 0x20 = data record   : [field0:u32 LE][sentinel:u32 LE][raw:4B]
#
# When live mode is on (after cmd 0x23 [0x01]), the device emits a 0x25
# header then one or more 0x20 records belonging to that block. The
# block's dataType scopes which OBIS register the records refer to.


@dataclass(frozen=True)
class BlockHeader:
    """Decoded cmd 0x25 block header — scopes the OBIS register for
    subsequent cmd 0x20 records in the same live-readout block."""

    data_type: int        # u8 — the OBIS register identifier as the device sees it
    timestamp: int        # u32 LE — unix seconds the block was emitted
    raw: bytes


def parse_block_header(payload: bytes) -> BlockHeader | None:
    """Decode cmd 0x25 payload.

    Layout: byte[0] = dataType, byte[1..5] = unix_ts LE.
    Frames with dataType == 0 are treated as keep-alives and dropped.
    """
    if len(payload) < 5:
        return None
    data_type = payload[0]
    if data_type == 0:
        return None
    timestamp = struct.unpack_from("<I", payload, 1)[0]
    return BlockHeader(data_type=data_type, timestamp=timestamp, raw=bytes(payload[:5]))


SENTINEL_NO_VALUE = 0xFFFFFFFF


@dataclass(frozen=True)
class DataRecord:
    """Decoded cmd 0x20 data record (12 bytes) — three little-endian u32s.

    ``sentinel == SENTINEL_NO_VALUE`` (0xFFFFFFFF) means "register has no
    value" — the integration treats those records as `unavailable` rather
    than zero. The semantics of ``field0`` aren't fully pinned down; it
    appears to be a sequence counter or sub-register selector.
    """

    field0: int           # u32 LE — purpose TBD (HYPOTHESIS)
    sentinel: int         # u32 LE — 0xFFFFFFFF means "no value"
    raw_value: bytes      # 4 bytes — meter reading; interpretation per dataType
    raw: bytes

    @property
    def has_value(self) -> bool:
        return self.sentinel != SENTINEL_NO_VALUE

    @property
    def value_u32(self) -> int:
        """Raw value as a little-endian u32. Use for generic display."""
        return int.from_bytes(self.raw_value, "little")


@dataclass(frozen=True)
class AutoDetectResult:
    """Decoded cmd 0x19 response (4 bytes):
      byte[0] = status enum
      byte[1] = detected protocol  (same ordinal as cmd 0x14)
      byte[2] = detected baud rate index
      byte[3] = parity / stop / extras
    """

    status: int
    protocol: int
    baud_index: int
    extras: int
    raw: bytes

    def summary(self) -> str:
        """Human-readable summary for the sensor state."""
        proto_name = {0: "IEC", 1: "SML", 2: "Blink", 3: "DLMS"}.get(
            self.protocol, f"proto={self.protocol}"
        )
        return f"{proto_name} (status={self.status}, baud_index={self.baud_index}, extras={self.extras})"


def parse_auto_detect(payload: bytes) -> AutoDetectResult | None:
    """Decode cmd 0x19 response."""
    if len(payload) < 4:
        return None
    return AutoDetectResult(
        status=payload[0],
        protocol=payload[1],
        baud_index=payload[2],
        extras=payload[3],
        raw=bytes(payload[:4]),
    )


def parse_data_record(payload: bytes) -> DataRecord | None:
    """Decode cmd 0x20 payload (12 bytes)."""
    if len(payload) < 12:
        return None
    field0, sentinel = struct.unpack_from("<II", payload, 0)
    raw_value = bytes(payload[8:12])
    return DataRecord(
        field0=field0,
        sentinel=sentinel,
        raw_value=raw_value,
        raw=bytes(payload[:12]),
    )
