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
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OneMeterCoordinator, OneMeterData
from .obis_map import KNOWN_OBIS, ObisDescriptor, scaled_value
from .protocol.decode import SENTINEL_NO_VALUE


@dataclass(frozen=True, kw_only=True)
class OneMeterSensorDescription(SensorEntityDescription):
    """Description of an OneMeter sensor."""

    value_fn: Callable[[OneMeterData], Any]


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
    async_add_entities(
        OneMeterObisSensor(coordinator, descriptor)
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
    ) -> None:
        super().__init__(coordinator)
        self._descriptor = descriptor
        self._attr_unique_id = f"{coordinator.address}_obis_{descriptor.key}"
        self._attr_name = descriptor.name
        self._attr_native_unit_of_measurement = descriptor.unit
        self._attr_device_class = descriptor.device_class
        self._attr_state_class = descriptor.state_class
        self._attr_entity_registry_enabled_default = (
            descriptor.entity_registry_enabled_default
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
