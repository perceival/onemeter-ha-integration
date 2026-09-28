"""OneMeter sensor entities."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricPotential, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PROSUMER, DOMAIN, MAX_RAW_OBIS_ENTITIES
from .coordinator import OneMeterCoordinator, OneMeterData
from .obis_map import KNOWN_OBIS, ObisDescriptor, scaled_value, to_utc_datetime
from .policy import (
    raw_obis_candidates,
    raw_obis_value,
    session_value,
    should_create_raw_obis,
)
from .protocol.advert import ADVERT_TAG_OBIS
from .protocol.decode import SENTINEL_NO_VALUE, format_obis
from .protocol.obis_labels import label_for

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class OneMeterSensorDescription(SensorEntityDescription):
    """Description of an OneMeter sensor."""

    value_fn: Callable[[OneMeterData], Any]
    # Optional extra state attributes, for diagnostic entities that surface a
    # whole map of values where one entity per value would be entity sprawl.
    attrs_fn: Callable[[OneMeterData], dict[str, Any]] | None = None


def _advert_clock_attrs(data: OneMeterData) -> dict[str, Any]:
    """Say where the advertised clock came from.

    Only the 24-byte data advert's clock is inside the CCM message; the 9-byte
    minimal advert carries it in the clear, so a value can be set by anything in
    radio range that knows this device's address. Worth surfacing rather than
    leaving the reader to assume the entity is authenticated.
    """
    return {
        "source": "data advert (authenticated)" if data.advert_clock_authenticated
        else "minimal advert (plaintext, unauthenticated)",
    }


def _advert_clock_dt(data: OneMeterData) -> datetime | None:
    """The advertised device clock as a tz-aware datetime.

    The clock is a raw u32 off the wire, so out-of-range values are possible
    and must degrade to None rather than raise inside a state update.
    """
    if data.advert_clock is None:
        return None
    return to_utc_datetime(data.advert_clock)


def _unmapped_advert_tags(data: OneMeterData) -> dict[str, Any]:
    """Broadcast register tags with no OBIS mapping yet, as {0xNN: raw value}."""
    return {
        f"0x{tag:02X}": value
        for tag, value in sorted(data.advert_records.items())
        if tag not in ADVERT_TAG_OBIS
    }


def _cached_registers(data: OneMeterData) -> dict[str, Any]:
    """Every cached OBIS register as {A.B.C.D: raw u32 value}.

    Includes codes with no descriptor (which also get a disabled-by-default raw
    entity of their own — see `OneMeterRawObisSensor`) and
    the device's "no value" sentinel, which shows up as 4294967295 — the point
    of this entity is to show what the device actually holds, unfiltered, so a
    meter's codes can be identified without downloading diagnostics.
    """
    return {
        format_obis(obis): entry.value
        for obis, entry in sorted(data.cached_obis_by_code.items())
    }


SENSORS: tuple[OneMeterSensorDescription, ...] = (
    OneMeterSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        name="Battery voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.battery_volts,
    ),
    OneMeterSensorDescription(
        key="serial",
        translation_key="serial",
        name="Serial number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: str(d.serial) if d.serial is not None else None,
    ),
    OneMeterSensorDescription(
        key="mac",
        translation_key="mac",
        name="BLE MAC",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.mac,
    ),
    OneMeterSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        name="Last seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_seen,
    ),
    OneMeterSensorDescription(
        key="rx_frames",
        translation_key="rx_frames",
        name="RX frames",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.rx_frames,
    ),
    OneMeterSensorDescription(
        key="rx_errors",
        translation_key="rx_errors",
        name="RX errors",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.rx_errors,
    ),
    OneMeterSensorDescription(
        key="rx_rejections",
        translation_key="rx_rejections",
        name="RX cipher rejections",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.rx_rejections,
    ),
    OneMeterSensorDescription(
        key="conn_state",
        translation_key="conn_state",
        name="Connection state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.state,
    ),
    OneMeterSensorDescription(
        key="device_clock_drift",
        translation_key="device_clock_drift",
        name="Device clock drift",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.device_clock_drift_s,
    ),
    # --- Meter communication statistics (cmd 0x36) ---
    OneMeterSensorDescription(
        key="meter_reads_succeeded",
        translation_key="meter_reads_succeeded",
        name="Meter reads succeeded",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.comm_succeeded_total,
    ),
    OneMeterSensorDescription(
        key="meter_reads_failed",
        translation_key="meter_reads_failed",
        name="Meter reads failed",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.comm_failed_total,
    ),
    OneMeterSensorDescription(
        key="meter_day_cycles",
        translation_key="meter_day_cycles",
        name="Meter day cycles completed",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.comm_day_cycles_completed,
    ),
    OneMeterSensorDescription(
        key="last_protocol_set",
        translation_key="last_protocol_set",
        name="Last protocol set",
        entity_category=EntityCategory.DIAGNOSTIC,
        # NB: this is a memo of what the integration last *wrote* to the
        # device via cmd 0x14 — NOT a live read of the device's current
        # configured protocol. The device doesn't expose its current
        # setting via any known read command. Stays `unknown` until the
        # user changes the Meter Protocol option from "Leave unchanged".
        value_fn=lambda d: d.configured_protocol_name,
    ),
    # --- Passive (advertisement) reading ---
    OneMeterSensorDescription(
        key="data_source",
        translation_key="data_source",
        name="Data source",
        entity_category=EntityCategory.DIAGNOSTIC,
        # "active" = the last values came from a GATT session, "passive" =
        # decoded from the broadcast. Passive carries the energy registers and
        # the device clock only, so a mix across fields is normal.
        value_fn=lambda d: d.data_source,
        attrs_fn=_unmapped_advert_tags,
    ),
    OneMeterSensorDescription(
        key="advert_clock",
        translation_key="advert_clock",
        name="Advertised device clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_advert_clock_dt,
        attrs_fn=_advert_clock_attrs,
    ),
    OneMeterSensorDescription(
        key="adv_frames",
        translation_key="adv_frames",
        name="Advertisements decoded",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Deliberately no state_class: this counter is driven from the radio, so
        # it must not feed HA's long-term statistics.
        value_fn=lambda d: d.adv_frames,
    ),
    OneMeterSensorDescription(
        key="adv_errors",
        translation_key="adv_errors",
        name="Advertisements undecodable",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.adv_errors,
    ),
    OneMeterSensorDescription(
        key="cached_registers",
        translation_key="cached_registers",
        name="Cached OBIS registers",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The state is how many registers the device has reported; the
        # attributes carry the values themselves, keyed by A.B.C.D.
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.cached_obis_by_code),
        attrs_fn=_cached_registers,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OneMeterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OneMeterSensor(coordinator, description) for description in SENSORS
    )
    # Static OBIS-mapped sensors — one per known code. Always created
    # (even if the current cached_obis_by_code doesn't have a value
    # yet); their `available` reflects whether a value is present.
    is_prosumer = bool(entry.options.get(CONF_PROSUMER, False))
    async_add_entities(
        OneMeterObisSensor(coordinator, descriptor, is_prosumer=is_prosumer)
        for descriptor in KNOWN_OBIS.values()
    )

    # Dynamic per-dataType register sensors. We don't know which dataTypes
    # the meter will emit until live frames arrive. The coordinator owns
    # the register store; when a new dataType is observed, it calls back
    # to create the entity here.
    added_dts: set[int] = set()

    def _add_register_entity(data_type: int) -> None:
        if data_type in added_dts:
            return
        added_dts.add(data_type)
        async_add_entities([
            OneMeterRegisterSensor(coordinator, data_type),
            OneMeterRegisterTimestampSensor(coordinator, data_type),
        ])

    coordinator.register_add_register_entity_cb(_add_register_entity)
    # Add entities for any dataTypes already seen before sensor.py loaded
    # (e.g. on entry reload).
    for dt in coordinator.data.meter_registers:
        _add_register_entity(dt)

    # Raw sensors for discovered OBIS codes that have no descriptor of their own,
    # disabled by default. A meter reports many codes — some duplicated across
    # tariff slots, some meaningless to us — so enabling all of them would bury
    # the entity list; this exists so the ones you care about can be enabled
    # individually, and so an unmapped register is visible at all. The scaled
    # sensors above keep their own codes.
    # Lifetime allowance (`materialised`): seeded from the entity registry, so the
    # ceiling counts what the entry has ever created rather than what this setup
    # run happened to see — registry entries are permanent, and every restart or
    # options reload would otherwise mint up to MAX_RAW_OBIS_ENTITIES more for
    # codes the device reports only later.
    #
    # The per-setup duplicate guard is a *separate* set: a code that is already
    # materialised still has to be handed to the platform on every setup, or the
    # registry entry keeps the user's enable flag and name but never gets a state.
    raw_prefix = f"{coordinator.address}_obis_raw_"
    registry = entity_registry.async_get(hass)
    materialised: set[bytes] = {
        bytes.fromhex(e.unique_id[len(raw_prefix):])
        for e in entity_registry.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        if e.unique_id.startswith(raw_prefix)
        and len(e.unique_id[len(raw_prefix):]) == 8
        and all(c in "0123456789abcdef" for c in e.unique_id[len(raw_prefix):])
    }
    added_this_setup: set[bytes] = set()
    obis_cap_warned = False

    def _add_raw_obis_entity(obis: bytes) -> None:
        nonlocal obis_cap_warned
        if obis in added_this_setup:
            return
        if obis not in materialised:
            cached = coordinator.data.cached_obis_by_code.get(obis)
            if not should_create_raw_obis(
                obis=obis,
                known_codes=KNOWN_OBIS,
                value=cached.value if cached is not None else None,
                added_count=len(materialised),
                limit=MAX_RAW_OBIS_ENTITIES,
            ):
                if len(materialised) >= MAX_RAW_OBIS_ENTITIES and not obis_cap_warned:
                    obis_cap_warned = True
                    _LOGGER.warning(
                        "OneMeter %s: reached the cap of %d raw OBIS entities; "
                        "ignoring further register codes (most recent: %s). The "
                        "cap exists because the device picks these codes and each "
                        "one becomes a permanent entity.",
                        coordinator.address, MAX_RAW_OBIS_ENTITIES,
                        format_obis(obis),
                    )
                return
            materialised.add(obis)
        # Only a code we actually hand over counts as handled: a rejected one
        # (no reading yet, or over the cap) is reconsidered on the next offer, so
        # a code first seen valueless still gets its entity when a value arrives
        # — passive installs can be hours between sessions.
        added_this_setup.add(obis)
        # Hand it to the platform even when the registry entry already exists:
        # HA needs the entity object on every setup, and that is the only way an
        # enabled (or renamed) raw entity gets a state. Note a materialised code
        # that later gains a descriptor is deliberately still re-added (a live
        # diagnostic entity beats silently losing state); the scaled sensor is
        # created alongside it.
        async_add_entities([OneMeterRawObisSensor(coordinator, obis)])

    coordinator.register_add_obis_entity_cb(_add_raw_obis_entity)
    # Already-materialised codes are re-added too (their registry entry may be
    # user-enabled), so the candidate set is the cached ones within the remaining
    # allowance, plus everything materialised before.
    for obis in sorted(
        set(raw_obis_candidates(
            coordinator.data.cached_obis_by_code,
            KNOWN_OBIS,
            max(MAX_RAW_OBIS_ENTITIES - len(materialised), 0),
        ))
        | materialised
    ):
        _add_raw_obis_entity(obis)


def _device_info(coordinator: OneMeterCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        connections={("bluetooth", coordinator.address)},
        name=coordinator.entry.title,
        manufacturer="OneMeter",
        model="optical reader (nRF51822)",
    )


class OneMeterSensor(CoordinatorEntity[OneMeterCoordinator], SensorEntity):
    """Per-attribute sensor backed by OneMeterData."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OneMeterCoordinator,
        description: OneMeterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None:
            return None
        return attrs_fn(self.coordinator.data)


class _SessionRestoreMixin(RestoreSensor):
    """Keep a session-sourced reading across restarts until the next session.

    The cached register set only refreshes in a GATT session, so after a
    restart these sensors would sit unavailable for hours on a passive install.
    The last value saved at shutdown stands in until this runtime receives the
    register again; `policy.session_value` decides, and the `value_origin`
    attribute says which one is showing.
    """

    _restored_value: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        # A saved value is only reused if it still fits this sensor: a later
        # release may change a descriptor's device_class, unit or scale, and a
        # mismatched stand-in would fail the state write or show a mis-scaled
        # number until the next session.
        if (
            last is not None
            and last.native_unit_of_measurement == self.native_unit_of_measurement
            and self._restore_fits(last.native_value)
        ):
            self._restored_value = last.native_value

    def _restore_fits(self, value: Any) -> bool:
        """Whether a saved value has the type this sensor would produce itself."""
        raise NotImplementedError

    def _live(self) -> tuple[bool, Any]:
        """(the coordinator currently holds an entry for the register, its exposable value).

        This is the *current* view only; `_resolved` latches it, see there.
        """
        raise NotImplementedError

    def _resolved(self) -> tuple[Any, str | None]:
        live_seen, live = self._live()
        if live_seen:
            # The first live entry retires the stand-in for good. Otherwise a
            # later readout that omits this code would resurrect the pre-restart
            # value — for a TOTAL_INCREASING register a backwards step that the
            # statistics engine books as a meter reset.
            self._restored_value = None
        return session_value(live_seen=live_seen, live=live, restored=self._restored_value)

    # `available` / `native_value` are defined on the concrete classes, not
    # here: CoordinatorEntity precedes this mixin in their MRO and its own
    # `available` would shadow a definition placed on the mixin.

    def _origin_attrs(self) -> dict[str, Any]:
        origin = self._resolved()[1]
        return {"value_origin": origin} if origin else {}


class OneMeterObisSensor(CoordinatorEntity[OneMeterCoordinator], _SessionRestoreMixin):
    """Sensor backed by a cached-OBIS entry (cmd 0x21 response).

    One instance per `ObisDescriptor` in `obis_map.KNOWN_OBIS`. Looks
    up its value in `coordinator.data.cached_obis_by_code` keyed by
    the 4-byte OBIS code; the last value is restored across restarts
    until the next session (see `_SessionRestoreMixin`).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OneMeterCoordinator,
        descriptor: ObisDescriptor,
        is_prosumer: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self._descriptor = descriptor
        self._attr_unique_id = f"{coordinator.address}_obis_{descriptor.key}"
        self._attr_name = descriptor.name
        self._attr_native_unit_of_measurement = descriptor.unit
        self._attr_device_class = descriptor.device_class
        self._attr_state_class = descriptor.state_class
        # Prosumer-gated descriptors (e.g. energy_export_total) follow
        # the user's declared prosumer status instead of the static
        # default — see obis_map.ObisDescriptor.requires_prosumer.
        self._attr_entity_registry_enabled_default = (
            is_prosumer if descriptor.requires_prosumer
            else descriptor.entity_registry_enabled_default
        )
        self._attr_device_info = _device_info(coordinator)

    def _live(self) -> tuple[bool, Any]:
        entry = self.coordinator.data.cached_obis_by_code.get(self._descriptor.obis)
        if entry is None:
            return False, None
        if entry.value == SENTINEL_NO_VALUE:
            return True, None
        return True, scaled_value(self._descriptor, entry.value)

    def _restore_fits(self, value: Any) -> bool:
        if self._descriptor.device_class == SensorDeviceClass.TIMESTAMP:
            return isinstance(value, datetime)
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)

    @property
    def available(self) -> bool:
        return self._resolved()[0] is not None

    @property
    def native_value(self) -> Any:
        return self._resolved()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._origin_attrs() or None


class OneMeterRegisterSensor(CoordinatorEntity[OneMeterCoordinator], SensorEntity):
    """One auto-discovered meter register, scoped by `dataType`.

    The native value is the raw u32 reading. The dataType→OBIS mapping
    is meter-specific and not pinned down generically, so these sensors
    have no unit / scale / device_class. Users can rename and customise
    them via the UI.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OneMeterCoordinator, data_type: int) -> None:
        super().__init__(coordinator)
        self._data_type = data_type
        self._attr_unique_id = f"{coordinator.address}_meter_register_{data_type}"
        self._attr_name = f"Meter register {data_type}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        reg = self.coordinator.data.meter_registers.get(self._data_type)
        return reg is not None and reg.has_value

    @property
    def native_value(self) -> Any:
        reg = self.coordinator.data.meter_registers.get(self._data_type)
        if reg is None or not reg.has_value:
            return None
        return reg.value_u32


class OneMeterRegisterTimestampSensor(CoordinatorEntity[OneMeterCoordinator], SensorEntity):
    """Per-register timestamp diagnostic — when this register was last
    emitted in a 0x25 block header."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OneMeterCoordinator, data_type: int) -> None:
        super().__init__(coordinator)
        self._data_type = data_type
        self._attr_unique_id = f"{coordinator.address}_meter_register_{data_type}_ts"
        self._attr_name = f"Meter register {data_type} timestamp"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> Any:
        reg = self.coordinator.data.meter_registers.get(self._data_type)
        if reg is None or reg.last_seen_at is None:
            return None
        return reg.last_seen_at


class OneMeterRawObisSensor(CoordinatorEntity[OneMeterCoordinator], _SessionRestoreMixin):
    """One discovered OBIS register, exposed raw and disabled by default.

    Created for codes the device reports that have no descriptor of their own —
    those get a scaled `OneMeterObisSensor` instead. The value is the device's
    raw u32: an unmapped code's scale and unit are unknown, and inventing either
    would be worse than showing the number the device actually holds.

    Disabled by default because a meter exposes many codes and enabling them all
    would bury the useful entities. Enable the ones you care about under
    Settings -> Devices & Services -> Entities, then rename them as you like.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # No state_class on purpose, for the same reason the advert counters have
    # none: the unit and scale of an unmapped code are unknown, so these values
    # must not feed long-term statistics even when a user enables the entity.

    def _live(self) -> tuple[bool, Any]:
        """Unavailable while the device holds no reading, like the siblings."""
        entry = self.coordinator.data.cached_obis_by_code.get(self._obis)
        if entry is None:
            return False, None
        return True, raw_obis_value(entry.value)

    def _restore_fits(self, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @property
    def available(self) -> bool:
        return self._resolved()[0] is not None

    @property
    def native_value(self) -> int | None:
        return self._resolved()[0]

    def __init__(self, coordinator: OneMeterCoordinator, obis: bytes) -> None:
        super().__init__(coordinator)
        self._obis = obis
        self._attr_unique_id = f"{coordinator.address}_obis_raw_{obis.hex()}"
        # Codes we have identified get their name in parentheses, so the entity
        # list reads "OBIS <code> (<label>)" — see protocol/obis_labels.py.
        label = label_for(obis)
        self._has_label = label is not None
        self._attr_name = f"OBIS {format_obis(obis)}" + (f" ({label})" if label else "")
        self._attr_device_info = _device_info(coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # No raw_value attribute: it would only restate the state, and the
        # device's unfiltered value (sentinel included) is what
        # `cached_registers` is for. Attributes here say what the entity *is*.
        # The note must not read "no scale is known for this code" once the name
        # claims to know *which* register it is — that would invite reading a raw
        # value straight against a scaled kWh sibling.
        note = (
            "raw device units — the name identifies the register, but no scale "
            "is applied to this value"
            if self._has_label
            else "raw device units — no scale is known for this code"
        )
        return {"obis": format_obis(self._obis), "note": note, **self._origin_attrs()}
