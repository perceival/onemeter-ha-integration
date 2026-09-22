"""OneMeter custom integration entry point.

HA-side imports are done lazily inside ``async_setup_entry`` / ``async_unload_entry``
so that the protocol/ subpackage can be imported in unit tests without
pulling in the ``homeassistant`` runtime.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .const import ADV_MFR_ID, CONF_ADDRESS, DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import (
        BluetoothChange,
        BluetoothServiceInfoBleak,
    )
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import OneMeterCoordinator


def _register_passive_reader(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: OneMeterCoordinator
) -> None:
    """Feed this entry's advertisements to the coordinator, if configured.

    Matching is on the shared manufacturer ID (0xFFFF — every OneMeter unit uses
    it), with the per-device address checked in the callback below. Registered
    with PASSIVE scanning mode: we only ever read the device's own
    transmissions, so soliciting scan responses would add radio traffic without
    adding data.
    """
    from homeassistant.components import bluetooth
    from homeassistant.core import callback

    address = entry.data[CONF_ADDRESS]

    @callback
    def _on_advertisement(
        service_info: BluetoothServiceInfoBleak, _change: BluetoothChange
    ) -> None:
        # Address equality is the entire filter: two devices cannot share a
        # MAC, so this isolates exactly our unit. Deliberately *not* also
        # testing the advertised name — the local name normally arrives in a
        # scan response, which a PASSIVE scan never solicits, so a name test
        # here could silently disable the whole feature.
        if service_info.address.upper() != address.upper():
            return
        payload = service_info.manufacturer_data.get(ADV_MFR_ID)
        if payload:
            coordinator.async_handle_advertisement(bytes(payload))

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _on_advertisement,
            {"manufacturer_id": ADV_MFR_ID},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OneMeter from a config entry."""
    from .coordinator import OneMeterCoordinator  # noqa: PLC0415

    coordinator = OneMeterCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if coordinator.passive_enabled:
        _register_passive_reader(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "button"])

    # Reload the entry on options change so the new mode/poll-interval
    # takes effect immediately.
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))
    return True


async def _async_reload_on_options_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "button"])
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
