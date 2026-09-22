"""OneMeter sensor entities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricPotential, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PROSUMER, DOMAIN
from .coordinator import OneMeterCoordinator, OneMeterData
from .obis_map import KNOWN_OBIS, ObisDescriptor, scaled_value, to_utc_datetime
from .protocol.advert import ADVERT_TAG_OBIS
from .protocol.decode import SENTINEL_NO_VALUE, format_obis


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

    Includes codes with no descriptor (which get no entity of their own) and
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


class OneMeterObisSensor(CoordinatorEntity[OneMeterCoordinator], SensorEntity):
    """Sensor backed by a cached-OBIS entry (cmd 0x21 response).

    One instance per `ObisDescriptor` in `obis_map.KNOWN_OBIS`. Looks
    up its value in `coordinator.data.cached_obis_by_code` keyed by
    the 4-byte OBIS code.
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

    @property
    def available(self) -> bool:
        entry = self.coordinator.data.cached_obis_by_code.get(self._descriptor.obis)
        return entry is not None and entry.value != SENTINEL_NO_VALUE

    @property
    def native_value(self) -> Any:
        entry = self.coordinator.data.cached_obis_by_code.get(self._descriptor.obis)
        if entry is None or entry.value == SENTINEL_NO_VALUE:
            return None
        return scaled_value(self._descriptor, entry.value)


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
