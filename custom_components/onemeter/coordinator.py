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
import math
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
    ADVERT_NOTIFY_MIN_INTERVAL_S,
    AUTH_FAIL_THRESHOLD,
    CONF_ADDRESS,
    CONF_IV,
    CONF_KEY,
    CONF_METER_PROTOCOL,
    CONF_PASSIVE,
    CONF_PASSIVE_IV,
    CONF_PASSIVE_KEY,
    CONF_POLL_INTERVAL,
    DEFAULT_PASSIVE,
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
    PASSIVE_FALLBACK_S,
    PASSIVE_MAX_SESSION_GAP_S,
    PASSIVE_WAIT_S,
    POST_CONNECT_SETTLE_S,
    POST_SUBSCRIBE_SETTLE_S,
    RECONNECT_BACKOFF_MAX_S,
    RECONNECT_COOLDOWN_S,
    REJECTION_BACKOFF_FLOOR_S,
    RX_DEDUP_WINDOW_S,
    WRITE_TIMEOUT_S,
)
from . import policy
from .protocol import advert
from .protocol import commands as proto_cmds
from .protocol.decode import (
    AutoDetectResult,
    BlockHeader,
    DataRecord,
    DeviceTime,
    Identity,
    ObisEntry,
    format_mac,
    format_obis,
)
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
    PASSIVE = "passive"       # reading the broadcast only; no GATT link open


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
    # Device's own clock (cmd 0x1D) minus our clock at the moment of the
    # probe. Large drift can indicate the device's RTC isn't holding time
    # well, or that TIME_SYNC isn't reaching it.
    device_clock_drift_s: int | None = None
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
    # --- Passive (advertisement) reading ---
    # "active" = values came from a GATT session, "passive" = decoded from the
    # broadcast. Passive only carries the energy-register records + the device
    # clock, so a mix of both across fields is normal.
    data_source: str = "active"
    advert_clock: int | None = None           # device clock from the broadcast
    advert_quarter_hour: int | None = None    # 1-based, wraps at 96
    advert_records: dict[int, int] = field(default_factory=dict)  # tag -> raw value
    advert_last_seen: datetime | None = None       # last decodable advertisement
    advert_data_last_seen: datetime | None = None  # last one that carried records
    adv_frames: int = 0                       # advertisements decoded
    # Advertisements from *this* device that failed to decode: corruption, or a
    # payload shape this decoder doesn't know. Other units never reach the
    # handler (the bluetooth callback filters on this device's address), so a
    # rising count is a real signal about this device rather than radio noise.
    adv_errors: int = 0
    # True when advert_clock came from a 24-byte data advert (whose clock is
    # inside the CCM message) rather than the cleartext 9-byte one.
    advert_clock_authenticated: bool = False
    # Passive register values refused for sitting below the cached value. A
    # sustained run of these is the signal that separates a replayed
    # advertisement from a genuine device-side decrease.
    adv_regressions: int = 0


def _parse_passive_secret(value: str | None) -> bytes | None:
    """Decode a stored passive key/IV, treating anything malformed as absent.

    The config flow validates these, so a bad value here means the storage was
    edited by hand. Treating it as "not configured" — everything read over GATT,
    which is the pre-passive behaviour — is a better outcome than failing entry
    setup with a raw traceback, and it is the same fail-safe direction the
    advertisement callback depends on.
    """
    if not value:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    return raw if len(raw) == 16 else None


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
        self._add_obis_entity_cb = None
        self._pending_ack: asyncio.Event = asyncio.Event()
        self._last_ack_cmd: int | None = None
        # Set by _on_disconnect; awaited by _safe_disconnect to confirm
        # the proxy actually closed the link (not just that our local
        # client.disconnect() call returned). If this never fires after
        # we ask to disconnect, the proxy's slot is stuck-allocated.
        self._disconnected_event: asyncio.Event = asyncio.Event()
        # --- Passive (advertisement) reading ---
        # The slot-2 key/IV are optional: without them passive reading is
        # simply unavailable and everything is read over GATT, as before.
        passive_key = entry.data.get(CONF_PASSIVE_KEY)
        passive_iv = entry.data.get(CONF_PASSIVE_IV)
        self._passive_key = _parse_passive_secret(passive_key)
        self._passive_iv = _parse_passive_secret(passive_iv)
        if (passive_key and self._passive_key is None) or (
            passive_iv and self._passive_iv is None
        ):
            _LOGGER.warning(
                "OneMeter %s: stored passive key/IV is malformed — passive reading "
                "stays disabled and everything is read over GATT",
                self.address,
            )
        self._passive_preferred: bool = bool(opts.get(CONF_PASSIVE, DEFAULT_PASSIVE))
        # Monotonic stamp of the last advertisement that carried register
        # records. Deliberately *not* a wall-clock datetime: freshness must not
        # be fooled by an NTP step on the proxy, and it must only ever be set
        # where records actually arrived (see _passive_is_fresh).
        self._passive_data_at: float | None = None
        # Outcome of the most recent session attempt, so the policy can avoid
        # deferring a retry by the whole poll interval. See policy.next_action.
        self._last_session_failed = False
        # Coalesces advertisement-driven state writes (see _notify_advert_listeners).
        self._last_adv_notify_at: float | None = None
        # Last advertised clock accepted for the drift sensor — see the guard in
        # async_handle_advertisement.
        self._last_adv_clock: int | None = None
        # OBIS codes already warned about for a refused passive regression, so a
        # replay campaign logs once per code rather than continuously.
        self._regression_warned: set[bytes] = set()
        # Set by the manual-poll / auto-detect buttons so they still run a
        # session while passive data is current.
        self._force_active_once = False
        # True only while _connect_and_run_session is executing. The buttons
        # consult it so a request arriving mid-session doesn't queue a second,
        # back-to-back connection.
        self._in_session = False
        # Monotonic time of the last *successful* active session. Bounds how
        # often we may connect, whatever the passive stream is doing.
        self._last_active_session_at: float | None = None
        # Advertisement tags seen so far, so the first sighting of an
        # unidentified register is logged once rather than every advert.
        self._seen_adv_tags: set[int] = set()
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

        Safe to call from any HA thread; only sets an event. A request that
        arrives while a session is already running is a no-op — that session
        refreshes everything a fresh one would, and queueing another here would
        produce two connections back-to-back, which is what the device's
        post-session cooldown punishes. While passive mode is active a request
        does force a real connection, bypassing the passive-first gate once.
        """
        if self._in_session:
            # Say so rather than discarding an explicit action silently: the
            # in-flight session runs the same probe set, so the request is
            # satisfied — but the user pressed a button and should be able to
            # find out why nothing appeared to happen.
            _LOGGER.info(
                "OneMeter %s: 'Poll now' arrived while a session was already running "
                "— that session refreshes the same values, so no second connection "
                "is made",
                self.address,
            )
            return
        self._force_active_once = True
        self._wake_now.set()

    def async_request_auto_detect(self) -> None:
        """Trigger cmd 0x19 (meter auto-detect) on the next session.

        Caller is expected to have validated availability (button gated
        on comm_succeeded_total > 0). Result populates data.detected_meter.
        """
        self._auto_detect_pending = True
        # Always force, even mid-session: the pending flag is consumed by
        # whichever session reaches its cmd 0x19 step, so if the in-flight one
        # already passed it the request would otherwise sit parked until the
        # broadcast went stale. The mandatory RECONNECT_COOLDOWN_S still bounds
        # the worst case to one extra connection for an explicit user press.
        self._force_active_once = True
        self._wake_now.set()

    def register_add_register_entity_cb(self, cb) -> None:
        """Wired by sensor.py during platform setup. Coordinator calls
        this when a new `dataType` is observed for the first time, to
        create a sensor entity for it."""
        self._add_register_entity_cb = cb

    def register_add_obis_entity_cb(self, cb) -> None:
        """Wired by sensor.py during platform setup. Coordinator calls this
        with every OBIS code it caches, so sensor.py can create a
        disabled-by-default raw entity for the ones it has no sensor for."""
        self._add_obis_entity_cb = cb

    def _notify_obis_codes(self, codes: list[bytes]) -> None:
        """Offer cached OBIS codes to sensor.py for entity creation.

        Called on every OBIS update rather than only for genuinely new codes:
        sensor.py owns the created-set (and skips codes that already have a
        descriptor), so that guard lives in one place instead of two.
        """
        if self._add_obis_entity_cb is None:
            return
        for obis in codes:
            try:
                self._add_obis_entity_cb(obis)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "OneMeter %s: failed to add a raw entity for OBIS %s",
                    self.address, format_obis(obis),
                )

    # --- Passive (advertisement) reading ------------------------------------

    @property
    def passive_enabled(self) -> bool:
        """True when passive reading is configured and not switched off.

        The length checks matter beyond defence in depth: they are what keeps a
        malformed stored value from reaching `decrypt_advert`, which raises on
        a wrong-size key — and this is armed from HA's bluetooth callback, where
        nothing may raise.
        """
        return (
            self._passive_key is not None
            and self._passive_iv is not None
            and len(self._passive_key) == 16
            and len(self._passive_iv) == 16
            and self._passive_preferred
        )

    def async_handle_advertisement(self, payload: bytes) -> None:
        """Decode one advertisement and fold its values into the cached state.

        Called from HA's bluetooth callback, so this runs on the event loop for
        every matching advertisement (a data advert arrives roughly every
        20-40 s). Decoding is a single AES-CCM verify, and it returns
        immediately unless passive reading is configured.

        A payload that does not verify is counted and dropped, never raised.
        The callback already filtered to this device's address, so an
        undecodable payload means corruption or an advert shape this decoder
        does not know — not another unit, and not general RF noise.
        """
        if not self.passive_enabled:
            return
        try:
            adv = advert.decode_advertisement(payload, self._passive_key, self._passive_iv)
        except Exception:  # noqa: BLE001
            # This runs inside HA's bluetooth dispatch, so nothing may escape.
            # It should be unreachable — passive_enabled length-checks the keys
            # precisely so decrypt_advert cannot raise — but the invariant is
            # worth enforcing rather than assuming.
            self.data.adv_errors += 1
            _LOGGER.exception("OneMeter %s: advertisement decoding raised", self.address)
            self._notify_advert_listeners()
            return
        if adv is None:
            self.data.adv_errors += 1
            _LOGGER.debug(
                "OneMeter %s: undecodable advertisement (%d bytes)", self.address, len(payload)
            )
            self._notify_advert_listeners()
            return
        self.data.adv_frames += 1
        self.data.advert_clock = adv.clock
        # The clock is authenticated only in the 24-byte shape; the 9-byte
        # minimal advert carries it in the clear, so record which this was and
        # let the entity say so (the value itself is still worth showing — it is
        # the only signal available without a key).
        self.data.advert_clock_authenticated = bool(adv.records)
        self.data.advert_last_seen = datetime.now(timezone.utc)
        if adv.records:
            self.data.advert_records = {rec.tag: rec.value for rec in adv.records}
            self.data.advert_data_last_seen = self.data.advert_last_seen
            self.data.advert_quarter_hour = adv.quarter_hour
            # Freshness is stamped only *here*: the clock-only advert carries no
            # registers, so it must not be able to hold the active path off.
            self._passive_data_at = time.monotonic()
            # Drift is written only for a clock that advanced. A replayed
            # advertisement re-verifies forever, and drift = clock - now differs
            # on every replay, so an ungated write would let a replayer feed
            # HA's long-term statistics (this sensor is a MEASUREMENT) a bogus
            # sample per throttle window for as long as they keep replaying. A
            # genuine backwards re-sync leaves the passive value stale until the
            # next session corrects it, which the fallback cap bounds to 6 h.
            if self._last_adv_clock is None or adv.clock >= self._last_adv_clock:
                self._last_adv_clock = adv.clock
                self.data.device_clock_drift_s = adv.clock - int(time.time())
            self._apply_obis_values(adv.obis_values())
            self.data.data_source = "passive"
            new_tags = {rec.tag for rec in adv.records} - self._seen_adv_tags
            if new_tags:
                self._seen_adv_tags |= new_tags
                _LOGGER.info(
                    "OneMeter %s: advertisement carries register tag(s) %s "
                    "(unidentified tags are reported so they can be mapped)",
                    self.address,
                    ", ".join(
                        f"0x{t:02X}{'' if t in advert.ADVERT_TAG_OBIS else ' (unmapped)'}"
                        for t in sorted(new_tags)
                    ),
                )
        _LOGGER.debug(
            "OneMeter %s: passive advert clock=%d records=%s quarter_hour=%s",
            self.address, adv.clock,
            {f"0x{r.tag:02X}": r.value for r in adv.records} or "none",
            adv.quarter_hour,
        )
        self._notify_advert_listeners()

    def _notify_advert_listeners(self) -> None:
        """Push passive updates to HA, coalesced.

        Advertisement handling is driven from the radio, so a spoofed flood of
        keyless 9-byte adverts could otherwise cause a state write — a recorder
        row plus a re-render of every entity — per advert, which is ~50/s at
        legacy advertising rates. Real register content only changes about every
        15 minutes, so coalescing to one update per interval costs nothing and
        bounds that: the counters still count every advert, they are just
        published less often.
        """
        now = time.monotonic()
        if not policy.should_notify(
            last_notify_at=self._last_adv_notify_at,
            now=now,
            min_interval_s=ADVERT_NOTIFY_MIN_INTERVAL_S,
        ):
            return
        self._last_adv_notify_at = now
        self.async_set_updated_data(self.data)

    def _apply_obis_values(self, values: dict[bytes, int]) -> None:
        """Merge passively-decoded registers into the cached-OBIS store.

        Passive updates are partial — whichever tags the current rotation slot
        happens to carry — so they merge rather than replace, unlike a cmd 0x21
        response, which is the device's authoritative full set and overwrites
        this dict wholesale on the next active session.

        Merges never move a register backwards (that rule lives in
        `policy.merge_cached`): the broadcast carries no ordering information and
        a captured advertisement re-verifies forever, so a replay must not be
        able to make a TOTAL_INCREASING sensor regress and log a meter reset in
        HA's statistics. This does *not* make mirrored tag pairs (0x0c/0x0d,
        0x11/0x12) order-independent — `Advertisement.obis_values()` collapses
        them with the last record winning — but on real hardware both members
        carry the same value, and the guard still stops the survivor regressing
        the stored one.

        Note the monotonic rule is an assumption for the four mapped codes that
        have no descriptor yet (0x16, 0x1B, 0x56, 0x57). If one of those turns
        out not to be monotonic, add its descriptor and revisit before trusting
        it.

        `raw` is rebuilt as ``[obis][u32 LE]`` — the canonical wire form of an
        entry, so the value renders the same as a session-read one.
        """
        cached_raw = {o: e.value for o, e in self.data.cached_obis_by_code.items()}
        accepted = policy.merge_cached(cached_raw, values)
        skipped = set(values) - set(accepted)
        if skipped:
            # Surfaced loudly once per code, plus a counter: a sustained run of
            # these is the one signal that separates a replay attack from a
            # genuine device-side decrease, and DEBUG alone would hide both.
            self.data.adv_regressions += len(skipped)
            _LOGGER.debug(
                "OneMeter %s: ignoring passive regression(s) for %s",
                self.address, ", ".join(format_obis(o) for o in sorted(skipped)),
            )
            for obis in sorted(skipped):
                if obis not in self._regression_warned:
                    self._regression_warned.add(obis)
                    _LOGGER.warning(
                        "OneMeter %s: a passive value for %s was below the cached one "
                        "and was ignored — either a replayed advertisement or the device "
                        "genuinely decreased it; the next active session settles which. "
                        "(Ignored values so far this run: see the adv_regressions counter.)",
                        self.address, format_obis(obis),
                    )
        for obis, value in accepted.items():
            self.data.cached_obis_by_code[obis] = ObisEntry(
                obis=obis, value=value, raw=obis + value.to_bytes(4, "little")
            )
            _LOGGER.debug(
                "OneMeter %s: passive OBIS %s = %d", self.address, format_obis(obis), value
            )
        self._notify_obis_codes(list(accepted))

    # --- Coordinator protocol -----------------------------------------------

    async def _async_update_data(self) -> OneMeterData:  # noqa: D401
        """Return the latest snapshot. Connection lives elsewhere."""
        return self.data

    # --- Background connection task -----------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._wake_now.clear()
            force_active = self._force_active_once
            self._force_active_once = False

            # Everything about "should we touch the radio this iteration" lives
            # in policy.next_action so it can be unit-tested — see the module
            # docstring there for why each branch is ordered the way it is.
            action, wait_s = policy.next_action(
                passive_enabled=self.passive_enabled,
                passive_fresh=self._passive_is_fresh(),
                since_last_session_s=(
                    time.monotonic() - self._last_active_session_at
                    if self._last_active_session_at is not None
                    else math.inf
                ),
                force_active=force_active,
                # A failed attempt is never deferred by the poll interval: the
                # two-phase wait below already spaced it by the backoff or the
                # rejection floor. See policy.next_action.
                last_attempt_failed=self._last_session_failed,
                poll_interval_s=self._poll_interval_s,
                max_passive_gap_s=PASSIVE_MAX_SESSION_GAP_S,
                passive_wait_s=PASSIVE_WAIT_S,
            )
            if action is policy.Action.PASSIVE:
                self._set_state(ConnState.PASSIVE)
                _LOGGER.debug(
                    "OneMeter %s: passive data is current — staying off the GATT link",
                    self.address,
                )
            elif action is policy.Action.WAIT:
                _LOGGER.debug(
                    "OneMeter %s: next active session in %.0fs (broadcast quiet, "
                    "poll interval not elapsed)",
                    self.address, wait_s,
                )
            if action is not policy.Action.SESSION:
                if await self._interruptible_wait(wait_s):
                    return
                continue

            session_ok = False
            self._in_session = True
            try:
                await self._connect_and_run_session()
                session_ok = True
                self._last_active_session_at = time.monotonic()
                # Current values now come from the session, not the broadcast.
                self.data.data_source = "active"
                backoff = 1.0  # successful session resets backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("OneMeter %s: session error: %s", self.address, exc)
                self._set_state(ConnState.DISCONNECTED)
            finally:
                self._in_session = False
                self._last_session_failed = not session_ok
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
                if await self._interruptible_wait(remaining_s):
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
            self._notify_obis_codes([e.obis for e in event])
            self._last_ack_cmd = 0x21
            # Verbose per-entry logging so we can correlate against the
            # meter's display when first attaching one. Each ObisEntry is
            # 4 bytes of OBIS code + u32 LE value; we render the 4 bytes
            # as A.B.C.D for readability.
            for ent in event:
                sentinel = ent.value == 0xFFFFFFFF
                _LOGGER.info(
                    "OneMeter %s: cached OBIS entry  raw=%s  A.B.C.D=%s  value=%s",
                    self.address, ent.raw.hex(), format_obis(ent.obis),
                    "no-value (0xFFFFFFFF)" if sentinel else f"{ent.value} (0x{ent.value:08X})",
                )
        elif isinstance(event, AutoDetectResult):
            self.data.detected_meter = event.summary()
            self._last_ack_cmd = 0x19
            _LOGGER.info("OneMeter %s: auto-detect result: %s", self.address, event.summary())
        elif isinstance(event, DeviceTime):
            self.data.device_clock_drift_s = event.clock - int(time.time())
            self._last_ack_cmd = 0x1D
            _LOGGER.debug(
                "OneMeter %s: device clock=%d drift=%+ds",
                self.address, event.clock, self.data.device_clock_drift_s,
            )
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

    async def _interruptible_wait(self, timeout_s: float) -> bool:
        """Sleep up to `timeout_s`, waking early on stop or a manual request.

        Returns True when the coordinator is shutting down, so callers can
        return straight out of the run loop.
        """
        if timeout_s <= 0:
            return self._stop.is_set()
        stop_task = self.hass.async_create_task(self._stop.wait())
        wake_task = self.hass.async_create_task(self._wake_now.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, wake_task):
                if not t.done():
                    t.cancel()
        return self._stop.is_set()

    def _passive_is_fresh(self) -> bool:
        """True while the broadcast is still delivering register records.

        The stamp is only ever set where records actually arrived, so a device
        that falls back to emitting just the 9-byte clock-only advert drops out
        of freshness after PASSIVE_FALLBACK_S and the active path resumes. That
        *mixed* case is the one that matters — a device that emitted a data
        advert once and then went quiet must not latch the gate open.
        """
        if not self.passive_enabled or self._passive_data_at is None:
            return False
        return policy.is_fresh(
            stamp=self._passive_data_at,
            now=time.monotonic(),
            window_s=PASSIVE_FALLBACK_S,
        )

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
