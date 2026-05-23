"""OneMeter config flow: bluetooth auto-discovery + manual entry + reauth."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    ADV_NAME_PREFIX,
    CONF_IV,
    CONF_KEY,
    CONF_METER_PROTOCOL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_S,
    DOMAIN,
    MAX_POLL_INTERVAL_S,
    METER_PROTOCOL_UNCHANGED,
    MIN_POLL_INTERVAL_S,
)
from .protocol import commands as proto_cmds
from homeassistant.config_entries import ConfigEntry, OptionsFlow

PROTOCOL_CHOICES = {
    METER_PROTOCOL_UNCHANGED: "Leave unchanged (don't write to device)",
    str(proto_cmds.PROTOCOL_IEC): proto_cmds.PROTOCOL_NAMES[proto_cmds.PROTOCOL_IEC],
    str(proto_cmds.PROTOCOL_SML): proto_cmds.PROTOCOL_NAMES[proto_cmds.PROTOCOL_SML],
    str(proto_cmds.PROTOCOL_BLINK): proto_cmds.PROTOCOL_NAMES[proto_cmds.PROTOCOL_BLINK],
    str(proto_cmds.PROTOCOL_DLMS): proto_cmds.PROTOCOL_NAMES[proto_cmds.PROTOCOL_DLMS],
}

_LOGGER = logging.getLogger(__name__)


CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_KEY): str,
        vol.Required(CONF_IV): str,
        vol.Required(CONF_METER_PROTOCOL, default=METER_PROTOCOL_UNCHANGED): vol.In(PROTOCOL_CHOICES),
    }
)


def _normalize_hex32(value: str) -> str | None:
    """Accept colons/spaces in the input; return 32 lowercase hex chars, or None."""
    cleaned = value.replace(" ", "").replace(":", "").lower()
    if len(cleaned) != 32:
        return None
    try:
        bytes.fromhex(cleaned)
    except ValueError:
        return None
    return cleaned


class OneMeterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OneMeter."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._reauth_entry = None

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "OneMeterOptionsFlow":
        return OneMeterOptionsFlow()

    # --- Bluetooth discovery -------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not (discovery_info.name or "").startswith(ADV_NAME_PREFIX):
            return self.async_abort(reason="not_supported")
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_credentials()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a discovered device, or fall through to manual entry."""
        candidates: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass):
            if not (info.name or "").startswith(ADV_NAME_PREFIX):
                continue
            if info.address in self._configured_addresses():
                continue
            candidates[info.address] = f"{info.name} ({info.address})"

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            self._discovered_address = address
            self._discovered_name = (
                candidates.get(address, "").split(" (")[0] or f"OneMeter {address}"
            )
            return await self.async_step_credentials()

        if not candidates:
            # Nothing discovered — fall through to manual address entry.
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(candidates)}),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual MAC entry — for when discovery hasn't surfaced the device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            # Basic MAC validation.
            parts = address.replace("-", ":").split(":")
            if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789ABCDEF" for c in p) for p in parts):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                address = ":".join(parts)
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                self._discovered_address = address
                self._discovered_name = f"OneMeter {address}"
                return await self.async_step_credentials()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect mobKey + IV. On submit, create the entry.

        The actual end-to-end auth check happens when the coordinator
        starts. If credentials are wrong, the entry will surface a
        Reauth banner via ConfigEntryAuthFailed.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            key = _normalize_hex32(user_input[CONF_KEY])
            iv = _normalize_hex32(user_input[CONF_IV])
            if key is None:
                errors[CONF_KEY] = "invalid_hex"
            if iv is None:
                errors[CONF_IV] = "invalid_hex"
            if not errors:
                assert self._discovered_address is not None
                # Store credentials in entry data (durable, not exposed
                # to options). Protocol selection goes into options
                # because the user can change it later — start with
                # whatever they picked at setup.
                proto_choice = user_input.get(CONF_METER_PROTOCOL, METER_PROTOCOL_UNCHANGED)
                return self.async_create_entry(
                    title=self._discovered_name or self._discovered_address,
                    data={
                        CONF_ADDRESS: self._discovered_address,
                        CONF_KEY: key,
                        CONF_IV: iv,
                    },
                    options={CONF_METER_PROTOCOL: proto_choice},
                )

        return self.async_show_form(
            step_id="credentials",
            data_schema=CREDENTIALS_SCHEMA,
            description_placeholders={
                "address": self._discovered_address or "?",
                "name": self._discovered_name or "?",
            },
            errors=errors,
        )

    # --- Reauth --------------------------------------------------------------

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self._discovered_address = self._reauth_entry.data[CONF_ADDRESS]
        self._discovered_name = self._reauth_entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = _normalize_hex32(user_input[CONF_KEY])
            iv = _normalize_hex32(user_input[CONF_IV])
            if key is None:
                errors[CONF_KEY] = "invalid_hex"
            if iv is None:
                errors[CONF_IV] = "invalid_hex"
            if not errors:
                assert self._reauth_entry is not None
                new_data = {**self._reauth_entry.data, CONF_KEY: key, CONF_IV: iv}
                return self.async_update_reload_and_abort(self._reauth_entry, data=new_data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=CREDENTIALS_SCHEMA,
            description_placeholders={
                "address": self._discovered_address or "?",
                "name": self._discovered_name or "?",
            },
            errors=errors,
        )

    # --- Helpers -------------------------------------------------------------

    def _configured_addresses(self) -> set[str]:
        return {entry.data[CONF_ADDRESS] for entry in self._async_current_entries() if CONF_ADDRESS in entry.data}


class OneMeterOptionsFlow(OptionsFlow):
    """Options flow: poll interval + meter protocol.

    Changing the protocol choice causes cmd 0x14 to be sent on the next
    session — that's a flash-write on the device. The UI string makes
    that explicit.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            interval = int(user_input[CONF_POLL_INTERVAL])
            if interval < MIN_POLL_INTERVAL_S or interval > MAX_POLL_INTERVAL_S:
                errors[CONF_POLL_INTERVAL] = "interval_out_of_range"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_POLL_INTERVAL: interval,
                        CONF_METER_PROTOCOL: user_input[CONF_METER_PROTOCOL],
                    },
                )

        opts = self.config_entry.options or {}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_S),
                    ): int,
                    vol.Required(
                        CONF_METER_PROTOCOL,
                        default=opts.get(CONF_METER_PROTOCOL, METER_PROTOCOL_UNCHANGED),
                    ): vol.In(PROTOCOL_CHOICES),
                }
            ),
            errors=errors,
            description_placeholders={
                "min_s": str(MIN_POLL_INTERVAL_S),
                "max_s": str(MAX_POLL_INTERVAL_S),
            },
        )
