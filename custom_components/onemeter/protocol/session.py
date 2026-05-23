"""Transport-agnostic OneMeter session state machine.

This is the high-level API used by the HA coordinator. It is *only*
about the wire protocol — it does not own any BLE transport, it does not
know about connection state, it does not schedule anything. The caller
(the HA coordinator) drives it:

    session = OneMeterSession(key=..., iv=...)

    # On every (re)connection:
    for frame_bytes in session.build_login(uuid, unix_secs):
        await ble.write_tx(frame_bytes)

    # Periodically, to keep the session alive:
    await ble.write_tx(session.build_probe(CMD_BATTERY))

    # On every BLE notification:
    event = session.feed_rx(ble_notification_bytes)
    if event is not None:
        ...
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass

from . import commands
from .cipher import OneMeterCipher
from .decode import (
    AutoDetectResult,
    BlockHeader,
    CommStats,
    DataRecord,
    FSParams,
    Identity,
    ObisEntry,
    parse_auto_detect,
    parse_battery,
    parse_block_header,
    parse_comm_stats,
    parse_data_record,
    parse_fs_params,
    parse_identity,
    parse_last_obis,
)
from .framing import FrameError, Reassembled, Reassembler, Rejected, encrypt_request


@dataclass(frozen=True)
class BatteryReading:
    volts: float
    raw: bytes


@dataclass(frozen=True)
class UnknownResponse:
    """Successfully reassembled response we don't have a decoder for."""

    cmd: int
    payload: bytes


@dataclass(frozen=True)
class RejectionEvent:
    """Device returned the canned cmd 0xFF auth/state-mismatch frame."""

    length: int
    raw: bytes


@dataclass(frozen=True)
class FrameErrorEvent:
    """Reassembler raised; included so the coordinator can count + log."""

    message: str


RxEvent = (
    BatteryReading
    | Identity
    | CommStats
    | FSParams
    | AutoDetectResult   # cmd 0x19
    | BlockHeader        # cmd 0x25
    | DataRecord         # cmd 0x20
    | RejectionEvent
    | FrameErrorEvent
    | UnknownResponse
    | list  # list[ObisEntry] — for cmd 0x21 last-readout
)


class OneMeterSession:
    """Stateless w.r.t. BLE connection; stateful w.r.t. cipher chain.

    Holds:
      * The cipher (constant key+iv from config entry).
      * The reassembler (tracks an in-progress multi-fragment response).

    Does NOT hold connection state — the coordinator owns that.
    """

    def __init__(self, key: bytes, iv: bytes) -> None:
        self._cipher = OneMeterCipher(key=key, iv=iv)
        self._reassembler = Reassembler(self._cipher)

    # --- Request builders ----------------------------------------------------

    def build_login(self, peripheral_uuid: bytes, unix_secs: int | None = None) -> list[bytes]:
        """Build the three encrypted frames of the daily-use session login.

        Returns frames in send order: 0xAA, 0x13, 0x23.
        """
        if unix_secs is None:
            unix_secs = int(time.time())
        return [
            encrypt_request(
                self._cipher,
                commands.CMD_LOGIN,
                commands.login_payload(peripheral_uuid, unix_secs),
            ),
            encrypt_request(
                self._cipher,
                commands.CMD_TIME_SYNC,
                commands.time_sync_payload(unix_secs),
            ),
            encrypt_request(
                self._cipher,
                commands.CMD_START_READOUT,
                commands.start_readout_payload(),
            ),
        ]

    def build_keepalive(self) -> bytes:
        """Cheapest single-frame read used to keep the session alive (battery)."""
        return encrypt_request(self._cipher, commands.CMD_BATTERY, commands.empty_payload())

    def build_auto_detect(self) -> bytes:
        """Cmd 0x19 — ask the device to probe the optical port for a meter."""
        return encrypt_request(
            self._cipher, commands.CMD_AUTO_DETECT, commands.empty_payload()
        )

    def build_set_protocol(self, protocol_ordinal: int) -> bytes:
        """Cmd 0x14 — set meter protocol. **Persistent (flash write).**

        Only invoked when the user has explicitly chosen a protocol via the
        config / options flow. See commands.PROTOCOL_*.
        """
        return encrypt_request(
            self._cipher,
            commands.CMD_SET_PROTOCOL,
            commands.set_protocol_payload(protocol_ordinal),
        )

    def build_stop_readout(self) -> bytes:
        """Cmd 0x23 with empty payload — polite close.

        Stops the device's live-readout mode. Sent right before BLE
        disconnect by the coordinator, matching the mobile app's behaviour.
        """
        return encrypt_request(
            self._cipher, commands.CMD_STOP_READOUT, commands.stop_readout_payload()
        )

    def build_probe(self, cmd: int, payload: bytes = b"") -> bytes:
        """Build any read-probe frame. Cmd must be on the allow-list."""
        commands.assert_allowed(cmd)
        return encrypt_request(self._cipher, cmd, payload)

    # --- Response handling ---------------------------------------------------

    def reset_reassembler(self) -> None:
        """Discard any in-progress multi-frag state. Call after disconnect."""
        self._reassembler = Reassembler(self._cipher)

    def feed_rx(self, ct_frame: bytes) -> RxEvent | None:
        """Decrypt + reassemble one BLE notification frame.

        Returns ``None`` if more frames are needed for an in-progress
        multi-fragment response. Otherwise returns a typed event the
        coordinator can dispatch on.
        """
        try:
            result = self._reassembler.feed(ct_frame)
        except FrameError as e:
            # Reassembler resets itself on errors.
            return FrameErrorEvent(message=str(e))

        if result is None:
            return None
        if isinstance(result, Rejected):
            return RejectionEvent(length=result.length, raw=result.raw)
        return self._decode(result)

    # --- Internal ------------------------------------------------------------

    def _decode(self, msg: Reassembled) -> RxEvent:
        cmd = msg.cmd
        payload = msg.payload

        # Empty ACK — carries no data; return an UnknownResponse so the
        # coordinator can confirm "yes, the device acknowledged this cmd".
        if msg.is_ack:
            return UnknownResponse(cmd=cmd, payload=b"")

        if cmd == commands.CMD_BATTERY:
            # Direct single-frag response: payload is the 13-byte body slice
            # [1:14] of the frame. The 3 channels are at [0..3]; for the
            # battery sensor we use channel 0 only.
            volts = parse_battery(payload[:3])
            if volts is not None:
                return BatteryReading(volts=volts, raw=payload[:3])

        if cmd == commands.CMD_IDENTITY:
            # Multi-frag reassembled — payload is the full identity blob.
            ident = parse_identity(payload)
            if ident is not None:
                return ident

        if cmd == commands.CMD_COMM_STATS:
            stats = parse_comm_stats(payload)
            if stats is not None:
                return stats

        if cmd == commands.CMD_FS_PARAMS:
            fs = parse_fs_params(payload)
            if fs is not None:
                return fs

        if cmd == commands.CMD_LAST_OBIS:
            return parse_last_obis(payload)

        if cmd == commands.CMD_AUTO_DETECT:
            res = parse_auto_detect(payload)
            if res is not None:
                return res
            return UnknownResponse(cmd=cmd, payload=payload)

        # Live-stream frames — unsolicited, only arrive in live mode.
        # 0x25 = block header (scopes which dataType the following 0x20
        # records belong to); 0x20 = data record.
        if cmd == commands.CMD_LIVE_OBIS_B:  # 0x25
            hdr = parse_block_header(payload)
            if hdr is not None:
                return hdr
            return UnknownResponse(cmd=cmd, payload=payload)

        if cmd == commands.CMD_LIVE_OBIS_A:  # 0x20
            rec = parse_data_record(payload)
            if rec is not None:
                return rec
            return UnknownResponse(cmd=cmd, payload=payload)

        return UnknownResponse(cmd=cmd, payload=payload)


def all_probe_commands() -> Iterable[int]:
    """The set of read-probe cmd bytes the integration polls during a session."""
    return (
        commands.CMD_BATTERY,
        commands.CMD_IDENTITY,
        commands.CMD_COMM_STATS,
        commands.CMD_FS_PARAMS,
        commands.CMD_LAST_OBIS,
    )
