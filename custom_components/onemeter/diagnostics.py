"""Diagnostics dump for the OneMeter integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_IV, CONF_KEY, DOMAIN
from .coordinator import OneMeterCoordinator

REDACT = {CONF_KEY, CONF_IV}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: OneMeterCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "data": {
            "battery_volts": data.battery_volts,
            "serial": data.serial,
            "mac": data.mac,
            "identity_status": data.identity_status.hex() if data.identity_status else None,
            "comm_stats_raw": data.comm_stats_raw.hex() if data.comm_stats_raw else None,
            "fs_params_raw": data.fs_params_raw.hex() if data.fs_params_raw else None,
            "last_obis_entries": [
                {"obis": e.obis.hex(), "value": e.value} for e in data.last_obis_entries
            ],
            "last_seen": data.last_seen.isoformat() if data.last_seen else None,
            "rx_frames": data.rx_frames,
            "rx_errors": data.rx_errors,
            "rx_rejections": data.rx_rejections,
            "state": data.state,
        },
    }
