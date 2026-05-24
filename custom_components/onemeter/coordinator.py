"""OneMeter coordinator: owns the BLE connection lifecycle.

Design notes:

* The connection is driven by a long-running async task, not by the
  DataUpdateCoordinator's poll loop. The poll loop only returns
  `self.data` — fresh values are pushed in via the BLE notify callback
  or after a keepalive succeeds.
* The state machine matches `design/01_ha_integration.md`:
  DISCONNECTED -> CONNECTING -> HANDSHAKING -> AUTHENTICATED ->
  (POLLING loop) -> back to DISCONNECTED on any failure.
* Auth failures (cipher rejections) raise ConfigEntryAuthFailed after
  AUTH_FAIL_THRESHOLD consecutive occurrences, which surfaces a "Reauth
  needed" banner in the UI.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTH_FAIL_THRESHOLD,
    CONF_ADDRESS,
    CONF_IV,
    CONF_KEY,
    CONF_METER_PROTOCOL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_S,
    DOMAIN,
    EPSI_RX_CHAR,
    EPSI_TX_CHAR,
    EPSI_UUID_CHAR,
    FORCE_FRESH_MAX_S,
    FORCE_FRESH_QUIET_S,
    INTER_LOGIN_FRAME_S,
    LOGIN_TIMEOUT_S,
    METER_PROTOCOL_UNCHANGED,
    ONE_SHOT_PROBES,
    POST_CONNECT_SETTLE_S,
    POST_SUBSCRIBE_SETTLE_S,
    RECONNECT_BACKOFF_MAX_S,
    RECONNECT_COOLDOWN_S,
    REJECTION_BACKOFF_FLOOR_S,
    RX_DEDUP_WINDOW_S,
    WRITE_TIMEOUT_S,
)
from .protocol import commands as proto_cmds
from .protocol.decode import AutoDetectResult, BlockHeader, DataRecord, Identity, ObisEntry, format_mac
from .protocol.session import (
    BatteryReading,
    CommStats,
    FSParams,
    FrameErrorEvent,
    OneMeterSession,
    RejectionEvent,
    UnknownResponse,
)

_LOGGER = logging.getLogger(__name__)


class ConnState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    AUTHENTICATED = "authenticated"
    DRAINING = "draining"     # in force-fresh-read mode, listening for live frames
    COOLING = "cooling"       # short wait between failed attempts
    SLEEPING = "sleeping"     # long wait between successful polls (polled mode)


@dataclass
class MeterRegister:
    """Latest known value for one dataType (OBIS register, as the device
    sees it). Populated only when live-mode 0x20 frames have been received.
    """

    data_type: int
    value_u32: int | None         # last raw value (u32 LE), or None if "no value"
    block_timestamp: int | None   # unix seconds from the most recent 0x25 header that scoped this 0x20
    last_seen_at: datetime | None
    has_value: bool               # False when the device emitted the 0xFFFFFFFF sentinel


@dataclass
class OneMeterData:
    """Snapshot of last-known device values + diagnostic counters."""

    battery_volts: float | None = None
    serial: int | None = None
    mac: str | None = None
    identity_status: bytes | None = None
    # Decoded comm-stats fields (cmd 0x36). When None, the device hasn't
    # reported stats yet this session. `succeeded_total` is the key one:
    # > 0 means the device has talked to a meter at some point.
    comm_succeeded_total: int | None = None
    comm_failed_total: int | None = None
    comm_day_cycles_completed: int | None = None
    comm_failed_on_id: int | None = None
    comm_failed_on_data: int | None = None
    comm_succeeded_on_demand: int | None = None
    comm_failed_on_demand: int | None = None
    comm_hardware_readouts: int | None = None
    comm_software_readouts: int | None = None
    comm_stats_raw: bytes | None = None
    fs_params_raw: bytes | None = None
    configured_protocol_name: str | None = None  # human-readable, after we send 0x14
    detected_meter: str | None = None              # last cmd 0x19 result summary
    # Per-dataType register store, populated by cmd 0x20 records (live
    # mode). Key = dataType int, value = a small dict with the latest
    # raw value + sentinel + last_seen_at.
    meter_registers: dict[int, "MeterRegister"] = field(default_factory=dict)
    last_obis_entries: list = field(default_factory=list)
    # Indexed view of the same entries, keyed by the 4-byte OBIS code so
    # sensor entities can do O(1) lookup of "their" value. Updated on
    # every cmd 0x21 response.
    cached_obis_by_code: dict[bytes, "ObisEntry"] = field(default_factory=dict)
    last_seen: datetime | None = None
    rx_frames: int = 0
    rx_errors: int = 0
    rx_rejections: int = 0
    state: str = ConnState.DISCONNECTED.value


class OneMeterCoordinator(DataUpdateCoordinator[OneMeterData]):
    """Owns the BLE connection + session for one OneMeter device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.data[CONF_ADDRESS]}",
            # DataUpdateCoordinator's poll loop is unused — connection is
            # driven by a background task. We set a long interval so HA
            # doesn't spam our _async_update_data, which only returns
            # self.data.
            update_interval=None,
        )
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self._key = bytes.fromhex(entry.data[CONF_KEY])
        self._iv = bytes.fromhex(entry.data[CONF_IV])
        opts = entry.options or {}
        self._poll_interval_s: float = float(opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_S))
        # Meter protocol option — stored as a string from the config flow
        # ("unchanged" or an ordinal digit). Coordinator tracks the last
        # value it has already sent so it doesn't re-send 0x14 every poll.
        self._desired_protocol: str = opts.get(CONF_METER_PROTOCOL, METER_PROTOCOL_UNCHANGED)
        self._last_sent_protocol: str | None = None
        self._session = OneMeterSession(self._key, self._iv)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake_now = asyncio.Event()
        self._client: BleakClient | None = None
        self._state = ConnState.DISCONNECTED
        self._consecutive_auth_fails = 0
        self._stuck_notification_fired = False
        # True when the previous session ended in a login rejection
        # (vs. a BLE-level error). The run loop uses this to apply a
        # longer backoff floor — see REJECTION_BACKOFF_FLOOR_S.
        self._last_failure_was_rejection = False
        # RX dedup: track the last ciphertext + arrival time so we can
        # drop the device's spurious duplicate notifications.
        self._last_dedup_ct: bytes | None = None
        self._last_dedup_at: float = 0.0
        # Live-stream session state. Populated as 0x25 (block headers)
        # arrive; consumed by 0x20 (data records) to scope which
        # dataType the record belongs to. Reset on every new session.
        self._current_block: BlockHeader | None = None
        # Used by the drain loop to detect "quiet window" via last-frame
        # timestamp. Every session ends with a brief drain so the meter
        # can push live data before disconnect; the quiet-window rule
        # caps the wait at ~2 s when nothing's arriving.
        self._auto_detect_pending = False
        self._last_live_frame_at: float = 0.0
        # The HA add-entity callback registered by sensor.py — used to
        # create a new register entity the first time a new dataType
        # arrives. Wired by the sensor platform during setup.
        self._add_register_entity_cb = None
        self._pending_ack: asyncio.Event = asyncio.Event()
        self._last_ack_cmd: int | None = None
        # Set by _on_disconnect; awaited by _safe_disconnect to confirm
        # the proxy actually closed the link (not just that our local
        # client.disconnect() call returned). If this never fires after
        # we ask to disconnect, the proxy's slot is stuck-allocated.
        self._disconnected_event: asyncio.Event = asyncio.Event()
        self.data = OneMeterData(state=self._state.value)

    # --- Public lifecycle ----------------------------------------------------

    async def async_start(self) -> None:
        """Spawn the long-running connection task."""
        self._task = self.hass.async_create_background_task(
            self._run(), name=f"onemeter_{self.address}"
        )

    async def async_stop(self) -> None:
        """Tear down: signal the task to stop, wait, and disconnect cleanly."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._safe_disconnect()

    def async_request_poll_now(self) -> None:
        """Interrupt the current sleep and start a poll session immediately.

        Safe to call from any HA thread; only sets an event. If a session
        is already running, this is a no-op (the next sleep cycle will
        see the event and run immediately).
        """
        self._wake_now.set()

    def async_request_auto_detect(self) -> None:
        """Trigger cmd 0x19 (meter auto-detect) on the next session.

        Caller is expected to have validated availability (button gated
        on comm_succeeded_total > 0). Result populates data.detected_meter.
        """
        self._auto_detect_pending = True
        self._wake_now.set()

    def register_add_register_entity_cb(self, cb) -> None:
        """Wired by sensor.py during platform setup. Coordinator calls
        this when a new `dataType` is observed for the first time, to
        create a sensor entity for it."""
        self._add_register_entity_cb = cb

    # --- Coordinator protocol -----------------------------------------------

    async def _async_update_data(self) -> OneMeterData:  # noqa: D401
        """Return the latest snapshot. Connection lives elsewhere."""
        return self.data

    # --- Background connection task -----------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._wake_now.clear()
            session_ok = False
            try:
                await self._connect_and_run_session()
                session_ok = True
                backoff = 1.0  # successful session resets backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("OneMeter %s: session error: %s", self.address, exc)
                self._set_state(ConnState.DISCONNECTED)
            finally:
                await self._safe_disconnect()

            # Two-phase wait:
            #   Phase 1 (COOLING) — mandatory device-cooldown, NOT
            #     interruptible by the manual-poll button. The device needs
            #     ~5 s after disconnect before it re-advertises; trying to
            #     reconnect during this window risks rejections.
            #   Phase 2 (SLEEPING on success / continued COOLING on failure)
            #     — the remainder of the poll interval (or the backoff on
            #     failure), interruptible by the button.
            if session_ok:
                total_wait_s = self._poll_interval_s
            else:
                # Choose a backoff floor based on what failed:
                #   - cipher rejection → device-side cooldown; rapid
                #     retries achieve nothing. Use REJECTION_BACKOFF_FLOOR_S.
                #   - BLE-level error / timeout → could be transient (RF
                #     interference, proxy hiccup); use the shorter
                #     RECONNECT_COOLDOWN_S floor.
                floor = (
                    REJECTION_BACKOFF_FLOOR_S
                    if self._last_failure_was_rejection
                    else RECONNECT_COOLDOWN_S
                )
                total_wait_s = max(floor, backoff)
                backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX_S)

            # Phase 1: mandatory cooldown.
            self._set_state(ConnState.COOLING)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONNECT_COOLDOWN_S)
                return  # stop signalled
            except asyncio.TimeoutError:
                pass

            # Phase 2: interruptible remainder (success path only — on
            # failure we don't enter SLEEPING, we just continue cooling).
            remaining_s = max(0.0, total_wait_s - RECONNECT_COOLDOWN_S)
            if remaining_s > 0:
                if session_ok:
                    self._set_state(ConnState.SLEEPING)
                stop_task = self.hass.async_create_task(self._stop.wait())
                wake_task = self.hass.async_create_task(self._wake_now.wait())
                try:
                    await asyncio.wait(
                        {stop_task, wake_task},
                        timeout=remaining_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (stop_task, wake_task):
                        if not t.done():
                            t.cancel()
                if self._stop.is_set():
                    return

    async def _connect_and_run_session(self) -> None:
        """One pass of: connect, handshake, keepalive loop until disconnect."""
        self._set_state(ConnState.CONNECTING)
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            raise BleakError(f"OneMeter {self.address} not currently in range of any proxy")

        self._disconnected_event.clear()
        client = await establish_connection(
            BleakClient,
            ble_device,
            f"onemeter-{self.address}",
            disconnected_callback=self._on_disconnect,
            max_attempts=2,
        )
        self._client = client
        # NOTE: do not clear self._client here. _run()'s finally block
        # calls _safe_disconnect() which needs self._client to actually
        # tear down the BLE link. _safe_disconnect is the single owner
        # of clearing self._client.
        await self._login_and_loop(client)

    async def _login_and_loop(self, client: BleakClient) -> None:
        # Settle window after BLE link-up before talking GATT. The device
        # needs ~1.5 s of post-connect time before it'll accept the first
        # encrypted write; sending cmd 0xAA earlier gets a canned
        # 0xFF rejection.
        await asyncio.sleep(POST_CONNECT_SETTLE_S)

        # Subscribe to the RX/indicate characteristic.
        await client.start_notify(EPSI_RX_CHAR, self._on_notify)
        await asyncio.sleep(POST_SUBSCRIBE_SETTLE_S)

        # Read the plaintext peripheral UUID. We only need bytes 0..8.
        peripheral_uuid = bytes(await client.read_gatt_char(EPSI_UUID_CHAR))
        if len(peripheral_uuid) < 9:
            raise BleakError(f"peripheral UUID too short: {peripheral_uuid.hex()}")

        # Handshake.
        self._set_state(ConnState.HANDSHAKING)
        self._session.reset_reassembler()
        # New session — discard any stale block header from a prior
        # session (we don't want to scope this session's 0x20 records
        # under the wrong dataType).
        self._current_block = None
        login_frames = self._session.build_login(peripheral_uuid)
        labels = ("login(0xAA)", "time_sync(0x13)", "start_readout(0x23)")
        try:
            for i, frame in enumerate(login_frames):
                if i > 0:
                    await asyncio.sleep(INTER_LOGIN_FRAME_S)
                await self._write_and_wait_ack(
                    client, frame, timeout=LOGIN_TIMEOUT_S / len(login_frames), label=labels[i]
                )
        except _AuthRejected:
            self._consecutive_auth_fails += 1
            self._last_failure_was_rejection = True
            if (
                self._consecutive_auth_fails == AUTH_FAIL_THRESHOLD
                and not self._stuck_notification_fired
            ):
                # First time we cross the threshold this session — surface
                # a notification suggesting the user wait. This is most
                # often the device's post-session cooldown state, NOT
                # wrong credentials. We do NOT trigger reauth — the
                # canonical `0xFF 0x01` rejection is the same regardless
                # of cause, and credentials don't change spontaneously.
                self._fire_stuck_notification()
                self._stuck_notification_fired = True
            raise BleakError("login rejected; will retry")
        else:
            self._consecutive_auth_fails = 0
            self._last_failure_was_rejection = False
            if self._stuck_notification_fired:
                # Recovery — clear the persistent notification.
                self._clear_stuck_notification()
                self._stuck_notification_fired = False

        # Reset auth counter on successful login.
        self._set_state(ConnState.AUTHENTICATED)

        # If the user has selected a meter protocol via the options flow
        # AND we haven't sent that selection yet, send cmd 0x14 now.
        # This is the only persistent (flash-write) command the
        # integration ever sends.
        if (
            self._desired_protocol != METER_PROTOCOL_UNCHANGED
            and self._desired_protocol != self._last_sent_protocol
        ):
            try:
                ordinal = int(self._desired_protocol)
                _LOGGER.info(
                    "OneMeter %s: applying meter-protocol option (ordinal=%d / %s) via cmd 0x14",
                    self.address, ordinal,
                    proto_cmds.PROTOCOL_NAMES.get(ordinal, "?"),
                )
                await self._write_and_wait_ack(
                    client,
                    self._session.build_set_protocol(ordinal),
                    timeout=WRITE_TIMEOUT_S,
                    label=f"set_protocol(0x14,ord={ordinal})",
                )
                self._last_sent_protocol = self._desired_protocol
                self.data.configured_protocol_name = proto_cmds.PROTOCOL_NAMES.get(
                    ordinal, f"ord {ordinal}"
                )
                self.async_set_updated_data(self.data)
                await asyncio.sleep(INTER_LOGIN_FRAME_S)
            except (asyncio.TimeoutError, _AuthRejected, ValueError) as exc:
                _LOGGER.warning(
                    "OneMeter %s: failed to set protocol: %s — will retry next session",
                    self.address, exc,
                )

        # Battery + one-shot probes. Always send 0x18 (battery) since there
        # is no keepalive loop to fetch it otherwise.
        probes = (0x18, *ONE_SHOT_PROBES)
        for cmd in probes:
            try:
                await self._write_and_wait_ack(
                    client,
                    self._session.build_probe(cmd),
                    timeout=WRITE_TIMEOUT_S,
                    label=f"probe(0x{cmd:02X})",
                )
                await asyncio.sleep(INTER_LOGIN_FRAME_S)
            except (asyncio.TimeoutError, _AuthRejected) as exc:
                _LOGGER.debug("OneMeter %s: probe 0x%02X failed: %s", self.address, cmd, exc)

        # If an auto-detect was requested, send cmd 0x19 once and record
        # the result. Probe is fire-and-forget at session time; result is
        # consumed by the AutoDetectResult event handler.
        if self._auto_detect_pending:
            self._auto_detect_pending = False
            try:
                await self._write_and_wait_ack(
                    client,
                    self._session.build_auto_detect(),
                    timeout=WRITE_TIMEOUT_S,
                    label="auto_detect(0x19)",
                )
            except (asyncio.TimeoutError, _AuthRejected) as exc:
                _LOGGER.warning(
                    "OneMeter %s: auto-detect failed: %s", self.address, exc,
                )
            await asyncio.sleep(INTER_LOGIN_FRAME_S)

        # Every session ends with a brief drain. Live mode was enabled by
        # the standard login (cmd 0x23 [0x01]); after probes, we wait
        # briefly for the meter to push any live `0x25` / `0x20` frames
        # before disconnecting. Without a meter the drain exits in ~2 s
        # via the quiet-window rule.
        await self._drain_live_frames()

        # End-of-session summary at INFO so a single log filter captures
        # the key results per session.
        d = self.data
        registers = sorted(d.meter_registers.keys())
        _LOGGER.info(
            "OneMeter %s: === SESSION SUMMARY ===  battery=%sV  serial=%s  "
            "succeeded_total=%s  failed_total=%s  detected_meter=%s  "
            "cached_obis_entries=%d  discovered_dataTypes=%s",
            self.address,
            f"{d.battery_volts:.3f}" if d.battery_volts is not None else "?",
            d.serial,
            d.comm_succeeded_total,
            d.comm_failed_total,
            d.detected_meter or "(not probed)",
            len(d.last_obis_entries),
            registers if registers else "(none)",
        )

        # All probes done. Send the polite-close (cmd 0x23 with no
        # payload) before disconnect. This matches the mobile-app
        # behaviour and clears the device's live-mode flag.
        try:
            await self._write_and_wait_ack(
                client,
                self._session.build_stop_readout(),
                timeout=WRITE_TIMEOUT_S,
                label="polite_close(0x23)",
            )
        except (asyncio.TimeoutError, _AuthRejected) as exc:
            _LOGGER.debug("OneMeter %s: polite-close failed: %s", self.address, exc)

    async def _write_and_wait_ack(self, client: BleakClient, frame: bytes, *, timeout: float, label: str = "") -> None:
        """Write one frame and wait for any response notification.

        The protocol layer's session.feed_rx is what classifies responses
        (ack / data / rejection) and updates self.data; this method just
        sleeps until *some* response arrives or we time out.
        """
        _LOGGER.debug("OneMeter %s: TX %s ct=%s", self.address, label, frame.hex())
        self._pending_ack.clear()
        try:
            await asyncio.wait_for(
                client.write_gatt_char(EPSI_TX_CHAR, frame, response=True),
                timeout=WRITE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise
        try:
            await asyncio.wait_for(self._pending_ack.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise
        if self._last_ack_cmd == 0xFF:
            raise _AuthRejected()

    # --- BLE callbacks -------------------------------------------------------

    def _on_notify(self, _sender, data: bytearray) -> None:
        """Called from bleak's thread/loop when a notification arrives."""
        ct = bytes(data)
        # Dedup: the device emits every rejection notification twice
        # (~1 ms apart, same ciphertext). Drop the duplicate to keep
        # the visible counters and log noise sane.
        now = time.monotonic()
        if (
            self._last_dedup_ct == ct
            and now - self._last_dedup_at < RX_DEDUP_WINDOW_S
        ):
            _LOGGER.debug(
                "OneMeter %s: RX dedup'd (%.0f ms after identical frame)",
                self.address, (now - self._last_dedup_at) * 1000,
            )
            return
        self._last_dedup_ct = ct
        self._last_dedup_at = now

        self.data.rx_frames += 1
        _LOGGER.debug("OneMeter %s: RX ct=%s", self.address, ct.hex())
        event = self._session.feed_rx(ct)
        if event is None:
            # Multi-frag in progress.
            _LOGGER.debug("OneMeter %s:    -> multi-frag incomplete", self.address)
            return
        _LOGGER.debug("OneMeter %s:    -> event=%r", self.address, event)
        self._handle_event(event)
        self._pending_ack.set()

    def _handle_event(self, event) -> None:
        """Update self.data based on a fully-decoded RX event."""
        self.data.last_seen = datetime.now(timezone.utc)
        if isinstance(event, BatteryReading):
            self.data.battery_volts = event.volts
            self._last_ack_cmd = 0x18
        elif isinstance(event, Identity):
            self.data.serial = event.serial
            self.data.mac = format_mac(event.mac)
            self.data.identity_status = event.status
            self._last_ack_cmd = 0x87
        elif isinstance(event, CommStats):
            self.data.comm_stats_raw = event.raw
            self.data.comm_succeeded_total = event.succeeded_total
            self.data.comm_failed_total = event.failed_total
            self.data.comm_day_cycles_completed = event.day_cycles_completed
            self.data.comm_failed_on_id = event.failed_on_id
            self.data.comm_failed_on_data = event.failed_on_data
            self.data.comm_succeeded_on_demand = event.succeeded_on_demand
            self.data.comm_failed_on_demand = event.failed_on_demand
            self.data.comm_hardware_readouts = event.hardware_readouts
            self.data.comm_software_readouts = event.software_readouts
            self._last_ack_cmd = 0x36
        elif isinstance(event, FSParams):
            self.data.fs_params_raw = event.raw
            self._last_ack_cmd = 0x82
        elif isinstance(event, list):  # ObisEntry list
            self.data.last_obis_entries = event
            self.data.cached_obis_by_code = {e.obis: e for e in event}
            self._last_ack_cmd = 0x21
            # Verbose per-entry logging so we can correlate against the
            # meter's display when first attaching one. Each ObisEntry is
            # 4 bytes of OBIS code + u32 LE value; we attempt to render
            # the 4 bytes as A.B.C.D for readability.
            for ent in event:
                obis_bytes = ent.obis
                obis_str = (
                    f"{obis_bytes[0]}.{obis_bytes[1]}.{obis_bytes[2]}.{obis_bytes[3]}"
                    if len(obis_bytes) == 4 else obis_bytes.hex()
                )
                sentinel = ent.value == 0xFFFFFFFF
                _LOGGER.info(
                    "OneMeter %s: cached OBIS entry  raw=%s  A.B.C.D=%s  value=%s",
                    self.address, ent.raw.hex(), obis_str,
                    "no-value (0xFFFFFFFF)" if sentinel else f"{ent.value} (0x{ent.value:08X})",
                )
        elif isinstance(event, AutoDetectResult):
            self.data.detected_meter = event.summary()
            self._last_ack_cmd = 0x19
            _LOGGER.info("OneMeter %s: auto-detect result: %s", self.address, event.summary())
        elif isinstance(event, BlockHeader):
            # Scopes the dataType for subsequent 0x20 records.
            self._current_block = event
            self._last_live_frame_at = time.monotonic()
            _LOGGER.info(
                "OneMeter %s: 0x25 block header  dataType=%d  ts=%d (unix)  raw=%s",
                self.address, event.data_type, event.timestamp, event.raw.hex(),
            )
        elif isinstance(event, DataRecord):
            self._last_live_frame_at = time.monotonic()
            block = self._current_block
            dt = block.data_type if block is not None else None
            _LOGGER.info(
                "OneMeter %s: 0x20 data record  block_dataType=%s  field0=%d  sentinel=0x%08X  raw_value_le=%s (=%s)  raw=%s",
                self.address, dt, event.field0, event.sentinel,
                event.raw_value.hex(),
                "no-value" if not event.has_value else f"{event.value_u32} (0x{event.value_u32:08X})",
                event.raw.hex(),
            )
            self._handle_data_record(event)
        elif isinstance(event, RejectionEvent):
            self.data.rx_rejections += 1
            self._last_ack_cmd = 0xFF
        elif isinstance(event, FrameErrorEvent):
            self.data.rx_errors += 1
            self._last_ack_cmd = None
        elif isinstance(event, UnknownResponse):
            self._last_ack_cmd = event.cmd
        # Notify HA-side listeners (entities) that data may have changed.
        self.async_set_updated_data(self.data)

    async def _drain_live_frames(self) -> None:
        """Drain live-mode frames (cmd 0x25 / 0x20) until quiet or capped.

        Live mode is already on — the login sequence sent cmd 0x23 [0x01].
        We just keep listening past the probe phase: as long as fresh
        frames keep arriving (any kind — block headers, data records),
        we keep going. Once `FORCE_FRESH_QUIET_S` elapses with no new
        frame, drain ends and the caller proceeds to polite-close.
        Hard cap at `FORCE_FRESH_MAX_S` to prevent runaway.
        """
        self._set_state(ConnState.DRAINING)
        # Treat probe-phase activity as "we just heard something" so the
        # quiet timer resets cleanly when entering drain.
        self._last_live_frame_at = time.monotonic()
        start = time.monotonic()
        _LOGGER.info(
            "OneMeter %s: entering DRAINING (quiet=%.1fs, max=%.1fs)",
            self.address, FORCE_FRESH_QUIET_S, FORCE_FRESH_MAX_S,
        )
        while True:
            now = time.monotonic()
            if now - start >= FORCE_FRESH_MAX_S:
                _LOGGER.info("OneMeter %s: drain hit MAX cap (%.1fs)", self.address, now - start)
                return
            if now - self._last_live_frame_at >= FORCE_FRESH_QUIET_S:
                _LOGGER.info(
                    "OneMeter %s: drain reached quiet window (%.1fs since last frame, total %.1fs)",
                    self.address, now - self._last_live_frame_at, now - start,
                )
                return
            await asyncio.sleep(0.25)

    def _handle_data_record(self, rec: DataRecord) -> None:
        """Apply a cmd 0x20 record to the per-dataType register store.

        If no block header has been seen yet in this live session, we
        drop the record — without a dataType we don't know which
        register it belongs to.
        """
        block = self._current_block
        if block is None:
            _LOGGER.debug(
                "OneMeter %s: ignoring 0x20 record arriving before any 0x25 header",
                self.address,
            )
            return
        dt = block.data_type
        new_register = dt not in self.data.meter_registers
        self.data.meter_registers[dt] = MeterRegister(
            data_type=dt,
            value_u32=rec.value_u32 if rec.has_value else None,
            block_timestamp=block.timestamp,
            last_seen_at=datetime.now(timezone.utc),
            has_value=rec.has_value,
        )
        if new_register and self._add_register_entity_cb is not None:
            try:
                self._add_register_entity_cb(dt)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("OneMeter %s: failed to add register entity for dataType=%d", self.address, dt)
        _LOGGER.debug(
            "OneMeter %s: register update dt=%d value=%s sentinel=%s",
            self.address, dt,
            rec.value_u32 if rec.has_value else None,
            "no_value" if not rec.has_value else "ok",
        )

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Called by bleak when the BLE link drops."""
        _LOGGER.debug("OneMeter %s: BLE disconnected", self.address)
        self._set_state(ConnState.DISCONNECTED)
        self._disconnected_event.set()

    # --- Helpers -------------------------------------------------------------

    async def _safe_disconnect(self) -> None:
        """Disconnect and verify the proxy actually closed the link.

        `await client.disconnect()` can return cleanly even when the
        disconnect command was lost in flight (we saw this with a WiFi
        roam coinciding with the disconnect — the API socket ACK'd at
        TCP level but the proxy never received the command). To detect
        that case, we wait for the disconnected_callback to fire, which
        only happens when the proxy reports the link is actually closed.
        If that doesn't happen, the proxy's slot is stuck-allocated and
        no further sessions will succeed until the proxy restarts.
        """
        if self._client is None:
            return
        client = self._client
        self._client = None
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "OneMeter %s: BLE disconnect call timed out after %.1fs",
                self.address, time.monotonic() - t0,
            )
            return
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "OneMeter %s: BLE disconnect call failed after %.1fs: %s",
                self.address, time.monotonic() - t0, exc,
            )
            return

        try:
            await asyncio.wait_for(self._disconnected_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "OneMeter %s: disconnect callback didn't fire within 3.0s "
                "(call returned in %.2fs). The proxy may be holding a phantom "
                "connection — a proxy restart will be needed to recover.",
                self.address, time.monotonic() - t0,
            )
            return
        _LOGGER.debug(
            "OneMeter %s: BLE disconnect confirmed in %.2fs",
            self.address, time.monotonic() - t0,
        )

    # --- Stuck-device notification --------------------------------------

    def _stuck_notification_id(self) -> str:
        return f"onemeter_{self.address.replace(':', '_').lower()}_stuck"

    def _fire_stuck_notification(self) -> None:
        """Surface a UI notification suggesting the user wait it out.

        The OneMeter firmware appears to enter a state after each
        session where it refuses subsequent BLE connections for a
        period (observed roughly ~15 min, sometimes longer). Power-
        cycling the device clears it; otherwise it appears to clear on
        its own after some time. The integration keeps retrying with
        exponential backoff.

        We do NOT trigger reauth — the same `0xFF 0x01` rejection
        happens both for wrong credentials and for the stuck state, so
        reauth would be a guess.
        """
        from homeassistant.components import persistent_notification

        _LOGGER.warning(
            "OneMeter %s: %d consecutive login rejections — likely in post-session "
            "cooldown. The integration will keep retrying. See persistent notification.",
            self.address, self._consecutive_auth_fails,
        )
        persistent_notification.async_create(
            self.hass,
            (
                f"OneMeter device **{self.entry.title}** is not accepting connections "
                f"right now. The integration will keep retrying — this typically "
                f"clears on its own after a while.\n\n"
                f"If polling continues to fail after a full day, the AES key / IV may "
                f"be wrong. Open the integration's **Configure** dialog to re-enter "
                f"credentials.\n\n"
                f"(Failed login attempts so far: {self._consecutive_auth_fails})"
            ),
            title="OneMeter — connection stuck",
            notification_id=self._stuck_notification_id(),
        )

    def _clear_stuck_notification(self) -> None:
        from homeassistant.components import persistent_notification

        persistent_notification.async_dismiss(
            self.hass, self._stuck_notification_id()
        )
        _LOGGER.info(
            "OneMeter %s: recovered after stuck state — notification dismissed",
            self.address,
        )

    def _set_state(self, state: ConnState) -> None:
        self._state = state
        self.data.state = state.value
        # Don't push an update if we haven't set self.data yet (during __init__).
        try:
            self.async_set_updated_data(self.data)
        except Exception:  # noqa: BLE001
            pass


class _AuthRejected(Exception):
    """Internal sentinel — device returned a 0xFF rejection mid-write."""
