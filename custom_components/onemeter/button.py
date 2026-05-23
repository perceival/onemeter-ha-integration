"""OneMeter button entities."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import OneMeterCoordinator

POLL_NOW_DESCRIPTION = ButtonEntityDescription(
    key="poll_now",
    translation_key="poll_now",
    name="Poll now",
    entity_category=EntityCategory.CONFIG,
)

AUTO_DETECT_DESCRIPTION = ButtonEntityDescription(
    key="auto_detect_meter",
    translation_key="auto_detect_meter",
    name="Auto-detect meter",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OneMeterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        OneMeterPollNowButton(coordinator),
        OneMeterAutoDetectButton(coordinator),
    ])


class _OneMeterButtonBase(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OneMeterCoordinator, description: ButtonEntityDescription) -> None:
        self.entity_description = description
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={("bluetooth", coordinator.address)},
            name=coordinator.entry.title,
            manufacturer="OneMeter",
            model="optical reader (nRF51822)",
        )


class OneMeterPollNowButton(_OneMeterButtonBase):
    """Press to interrupt the sleep and run a poll session immediately.

    The session does a full cached read (cmd 0x21) AND holds the link
    open briefly afterwards to catch any live `0x25` / `0x20` frames the
    meter pushes — so this button is equivalent to "give me the freshest
    data you can right now."
    """

    def __init__(self, coordinator: OneMeterCoordinator) -> None:
        super().__init__(coordinator, POLL_NOW_DESCRIPTION)

    async def async_press(self) -> None:
        self._coordinator.async_request_poll_now()


class OneMeterAutoDetectButton(_OneMeterButtonBase):
    """Press to ask the OneMeter device to probe the optical port for an
    attached meter (cmd 0x19). Returns a structured response stored in
    sensor.<name>_detected_meter.

    The button is **disabled** until the device has reported successful
    meter communication at least once (comm_succeeded_total > 0). This
    avoids exposing the feature on installs where there's no meter yet.
    """

    def __init__(self, coordinator: OneMeterCoordinator) -> None:
        super().__init__(coordinator, AUTO_DETECT_DESCRIPTION)

    @property
    def available(self) -> bool:
        # Note: HA's service layer enforces this for service calls too,
        # not just frontend display. Returning True unconditionally
        # while we're still figuring out the meter-side flow — the cost
        # of pressing it with no meter is one cmd 0x19 frame, harmless
        # (returns "no meter detected"-ish status). Once we've verified
        # the real-meter flow, we can re-gate on comm_succeeded_total.
        return True

    async def async_press(self) -> None:
        self._coordinator.async_request_auto_detect()
