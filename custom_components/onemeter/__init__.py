"""OneMeter custom integration entry point.

HA-side imports are done lazily inside ``async_setup_entry`` / ``async_unload_entry``
so that the protocol/ subpackage can be imported in unit tests without
pulling in the ``homeassistant`` runtime.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OneMeter from a config entry."""
    from .coordinator import OneMeterCoordinator  # noqa: PLC0415

    coordinator = OneMeterCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

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
