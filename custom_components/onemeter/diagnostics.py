"""Diagnostics dump for the OneMeter integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_IV, CONF_KEY, CONF_PASSIVE_IV, CONF_PASSIVE_KEY, DOMAIN
from .coordinator import OneMeterCoordinator

REDACT = {CONF_KEY, CONF_IV, CONF_PASSIVE_KEY, CONF_PASSIVE_IV}


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
            "device_clock_drift_s": data.device_clock_drift_s,
            # Passive (advertisement) reading. `advert_records` is keyed by
            # firmware tag so unmapped registers can be reported and matched
            # later; unmapped tags simply have no OBIS translation yet.
            "data_source": data.data_source,
            "advert_clock": data.advert_clock,
            "advert_clock_authenticated": data.advert_clock_authenticated,
            "advert_quarter_hour": data.advert_quarter_hour,
            "advert_records": {
                f"0x{tag:02X}": value for tag, value in sorted(data.advert_records.items())
            },
            "advert_last_seen": (
                data.advert_last_seen.isoformat() if data.advert_last_seen else None
            ),
            # The field that answers "does the data advert keep flowing with no
            # connections at all?" — the open question passive mode rests on.
            "advert_data_last_seen": (
                data.advert_data_last_seen.isoformat()
                if data.advert_data_last_seen
                else None
            ),
            "adv_frames": data.adv_frames,
            "adv_errors": data.adv_errors,
            "adv_regressions": data.adv_regressions,
        },
    }
