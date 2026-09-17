"""OneMeter command bytes and payload builders.

A strict allow-list is enforced: building a frame for any cmd byte not in
``ALLOWED_DEV_COMMANDS`` raises immediately, before any bytes touch the
wire. This is a guardrail — we never want a typo or stray call to send
e.g. cmd 0x59 (factory reset) or any registration-flow cmd.
"""
from __future__ import annotations

import struct

# --- Read-only and RAM-only commands ----------------------------------------
# Verified safe to send to a registered device per analysis docs.
CMD_LOGIN = 0xAA            # session login (RAM-only state change)
CMD_TIME_SYNC = 0x13        # set device clock (RAM-only)
CMD_SET_PROTOCOL = 0x14     # set meter protocol; payload [0x01, ordinal].
                            # The leading 0x01 is a save-to-flash flag, so
                            # this command IS persistent. Only sent when
                            # the user explicitly changes the meter
                            # protocol option.
CMD_START_READOUT = 0x23    # payload [0x01] enables live readout (RAM)
CMD_STOP_READOUT = 0x23     # no payload stops live readout and acts as
                            # a polite close, clearing the device's
                            # live-mode flag.
CMD_BATTERY = 0x18          # battery voltage read
CMD_IDENTITY = 0x87         # device identity blob
CMD_COMM_STATS = 0x36       # meter-communication statistics
CMD_FS_PARAMS = 0x82        # filesystem / advertising params
CMD_LAST_OBIS = 0x21        # last cached OBIS readout
CMD_AUTO_DETECT = 0x19      # ask the device to probe its optical port
                            # for an attached meter. In practice the
                            # response outside of a registration context
                            # is a bare ACK with no detection payload;
                            # kept on the allow-list because sending it
                            # is harmless.
CMD_DEVICE_TIME = 0x1D      # read the device's own clock. Response is a
                            # 4-byte little-endian unix timestamp followed
                            # by a second 4-byte field whose meaning isn't
                            # pinned down (varies unpredictably run to run
                            # on hardware with nothing attached to the
                            # optical port — likely raw/noisy ADC data
                            # from the unconnected sensor, not a counter).
                            # Cheap, single-frame, no session/buffer
                            # overhead — useful as a lightweight
                            # liveness/clock-drift check.

# Unsolicited notifications we expect to receive (not sent).
CMD_LIVE_OBIS_A = 0x20
CMD_LIVE_OBIS_B = 0x25

# --- Known-real, undocumented commands: NOT on the send allow-list ----------
# Found and confirmed real (device replies with a distinct, non-error
# response; never triggers the canned 0xFF auth/state-mismatch rejection)
# via live probing across two physical devices on 2026-09-15/16, but their
# semantics aren't understood well enough yet to expose safely. Recorded
# here so future work doesn't have to rediscover them from scratch — do
# NOT add these to ALLOWED_DEV_COMMANDS without first working out what
# each one actually does and whether any has a persistent (flash-write)
# effect.
#   0x15 — empty-ACK response. Reset/clear-flag shaped.
#   0x16 — real getter; observed value has differed (0 vs 1) across
#          devices/sessions. Meaning unknown.
#   0x1A — accepts a 2-byte payload (tested with [0x00, 0x01]); replies
#          with a single-byte value. Possibly CRC/checksum related.
#   0x1E — accepts a 1-byte payload constrained to <0x10; replies with an
#          empty ACK. The one command in this list most likely to have a
#          *persistent* effect — treat with extra care if revisited.
#   0x22 — empty-ACK response. Confirmed identical across two devices.
#   0x27 — real getter, stable value across sessions/devices (observed:
#          9 with an empty/zero payload; any other payload byte gets a
#          generic "invalid parameter" error, not a per-index value).
#   0x50, 0x51 — real getters, both observed returning value 2 on two
#          separate physical devices.
#   0x86 — real command, uniquely answers with TWO response frames
#          instead of one. Confirmed *not* a precursor to CMD_IDENTITY
#          (0x87) — sending it first has zero effect on 0x87's payload.

#: Commands the integration is allowed to *send* during normal operation.
#: Anything outside this set raises at build time.
#:
#: Notes on persistence:
#:   - 0xAA / 0x13 / 0x23 / 0x18 / 0x87 / 0x36 / 0x82 / 0x21 / 0x1D: RAM-only or read-only.
#:   - 0x14 (set protocol): **persistent** — writes to flash. Only sent when the
#:     user explicitly changes the protocol option. This is acknowledged in
#:     the options-flow UI text.
ALLOWED_DEV_COMMANDS = frozenset({
    CMD_LOGIN,
    CMD_TIME_SYNC,
    CMD_SET_PROTOCOL,
    CMD_START_READOUT,
    CMD_BATTERY,
    CMD_AUTO_DETECT,
    CMD_IDENTITY,
    CMD_COMM_STATS,
    CMD_FS_PARAMS,
    CMD_LAST_OBIS,
    CMD_DEVICE_TIME,
})


class ForbiddenCommandError(ValueError):
    """Raised when code attempts to encode a non-allow-listed cmd byte."""


def assert_allowed(cmd: int) -> None:
    if cmd not in ALLOWED_DEV_COMMANDS:
        raise ForbiddenCommandError(
            f"cmd 0x{cmd:02X} is not on the allow-list "
            f"(allowed: {{{', '.join(f'0x{c:02X}' for c in sorted(ALLOWED_DEV_COMMANDS))}}})"
        )


# --- Payload builders --------------------------------------------------------

def login_payload(peripheral_uuid: bytes, unix_secs: int) -> bytes:
    """Build the 13-byte payload for cmd 0xAA.

    Args:
        peripheral_uuid: 16 bytes read plaintext from GATT char ac040006.
        unix_secs: current unix timestamp.
    """
    if len(peripheral_uuid) < 9:
        raise ValueError("peripheral_uuid must be at least 9 bytes")
    return peripheral_uuid[:9] + struct.pack("<I", unix_secs)


def time_sync_payload(unix_secs: int) -> bytes:
    """Build the 4-byte payload for cmd 0x13."""
    return struct.pack("<I", unix_secs)


def start_readout_payload() -> bytes:
    """Build the 1-byte payload for cmd 0x23 (start live readout)."""
    return b"\x01"


def stop_readout_payload() -> bytes:
    """Build the empty payload for cmd 0x23 (stop live readout / polite close)."""
    return b""


def empty_payload() -> bytes:
    """Empty payload for the read probes (0x18 / 0x87 / 0x36 / 0x82 / 0x21)."""
    return b""


# Meter protocol enum — ordinals match the OneMeter mobile app's Protocol enum.
PROTOCOL_IEC = 0
PROTOCOL_SML = 1
PROTOCOL_BLINK = 2
PROTOCOL_DLMS = 3

PROTOCOL_NAMES = {
    PROTOCOL_IEC: "IEC 62056-21 mode D",
    PROTOCOL_SML: "SML",
    PROTOCOL_BLINK: "Blink (LED pulse)",
    PROTOCOL_DLMS: "DLMS",
}


def set_protocol_payload(protocol_ordinal: int) -> bytes:
    """Build the 2-byte payload for cmd 0x14: ``[0x01, ord]``.

    The leading ``0x01`` byte is a save-to-flash flag — this command
    writes persistent state on the device.
    """
    if protocol_ordinal not in PROTOCOL_NAMES:
        raise ValueError(f"unknown protocol ordinal {protocol_ordinal}")
    return bytes([0x01, protocol_ordinal])
