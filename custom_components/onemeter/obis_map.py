"""Known OBIS codes for cached-OBIS sensors (cmd 0x21 response).

Each entry in cmd 0x21's response is 8 bytes: 4-byte OBIS code + u32 LE
value. This module maps the 4-byte codes to typed descriptors with HA-
side metadata (units, scale factor, device_class, etc.).

The scale factor for energy registers is 0.01 (each "unit" in the
register is 10 Wh = 0.01 kWh), confirmed against an Apator NORAX 3
(SK 16-072 MI-003). Other meter families may produce different codes
or scales; unknown codes are logged but don't get an entity created.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy


@dataclass(frozen=True)
class ObisDescriptor:
    """Static metadata for one known OBIS code."""

    obis: bytes
    key: str                            # entity unique_id suffix
    name: str                           # display name (Home Assistant entity name)
    unit: str | None
    scale: float                        # multiply raw u32 by this to get the SI value
    device_class: SensorDeviceClass | None
    state_class: SensorStateClass | None
    entity_registry_enabled_default: bool = True

    def format_obis(self) -> str:
        b = self.obis
        return f"{b[0]}.{b[1]}.{b[2]}.{b[3]}"


# Reference codes from an Apator NORAX 3 (SK 16-072 MI-003). The
# 0.15.8.X family is the "Stan licznika" (meter reading) group:
# 0.15.8.0 is the total across all tariffs, 0.15.8.{1,2,3} are the
# per-tariff energy registers.
_ENERGY_TOTAL = ObisDescriptor(
    obis=bytes([0, 15, 8, 0]),
    key="energy_total",
    name="Energy total",
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    scale=0.01,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
)

_ENERGY_TARIFF_1 = ObisDescriptor(
    obis=bytes([0, 15, 8, 1]),
    key="energy_tariff_1",
    name="Energy tariff 1",
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    scale=0.01,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
)

_ENERGY_TARIFF_2 = ObisDescriptor(
    obis=bytes([0, 15, 8, 2]),
    key="energy_tariff_2",
    name="Energy tariff 2",
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    scale=0.01,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    # Disabled by default — many installs use tariff 1 only.
    entity_registry_enabled_default=False,
)

_ENERGY_TARIFF_3 = ObisDescriptor(
    obis=bytes([0, 15, 8, 3]),
    key="energy_tariff_3",
    name="Energy tariff 3",
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    scale=0.01,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    entity_registry_enabled_default=False,
)

# `0.15.7.0` is the instantaneous-power register in standard OBIS, but
# its scale isn't pinned down yet — surfaced only in diagnostics for now.

# `0xff` is the vendor-specific OBIS A-field. `255.1.1.4` carries the
# device's "last successful meter read" timestamp as a unix u32.
_LAST_READ_TS = ObisDescriptor(
    obis=bytes([0xFF, 0x01, 0x01, 0x04]),
    key="last_meter_read",
    name="Last meter read",
    unit=None,
    scale=1.0,
    device_class=SensorDeviceClass.TIMESTAMP,
    state_class=None,
)


KNOWN_OBIS: dict[bytes, ObisDescriptor] = {
    d.obis: d for d in (
        _ENERGY_TOTAL,
        _ENERGY_TARIFF_1,
        _ENERGY_TARIFF_2,
        _ENERGY_TARIFF_3,
        _LAST_READ_TS,
    )
}


def lookup(obis: bytes) -> ObisDescriptor | None:
    return KNOWN_OBIS.get(obis)


def scaled_value(descriptor: ObisDescriptor, raw: int) -> Any:
    """Apply the descriptor's scale to a raw u32 value, with type-aware coercion.

    For timestamp device_class, returns a `datetime` in UTC instead of a
    scaled number — the raw value is interpreted as unix seconds.
    """
    if descriptor.device_class == SensorDeviceClass.TIMESTAMP:
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if descriptor.scale == 1.0:
        return raw
    return raw * descriptor.scale
