"""OneMeter config flow: bluetooth auto-discovery + manual entry + reauth."""
from __future__ import annotations

import logging
import re
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
    CONF_PASSIVE,
    CONF_PASSIVE_IV,
    CONF_PASSIVE_KEY,
    CONF_POLL_INTERVAL,
    CONF_PROSUMER,
    DEFAULT_PASSIVE,
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
        # Optional: the broadcast channel's own key pair. Without it the
        # integration reads everything over GATT, exactly as before — with it,
        # passive reading can replace most connects (see const.py).
        vol.Optional(CONF_PASSIVE_KEY, default=""): str,
        vol.Optional(CONF_PASSIVE_IV, default=""): str,
        vol.Required(CONF_METER_PROTOCOL, default=METER_PROTOCOL_UNCHANGED): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_PROSUMER, default=False): bool,
    }
)


def _normalize_optional_hex32(value: str, errors: dict[str, str], field: str) -> str | None:
    """Normalize an optional 16-byte hex field; blank means "not provided".

    Returns PASSIVE_CLEAR_TOKEN unchanged when the user asked for removal, else
    the normalized hex, else None. Records ``invalid_hex`` under `field` when a
    value is present but malformed, so the caller can just check `errors`.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if cleaned == PASSIVE_CLEAR_TOKEN:
        return PASSIVE_CLEAR_TOKEN
    normalized = _normalize_hex32(cleaned)
    if normalized is None:
        errors[field] = "invalid_hex"
    return normalized


_HEX32_RE = re.compile(r"[0-9a-f]{32}")

# Entering this instead of a passive key/IV removes a stored pair; blank means
# "leave whatever is stored alone". Without it there would be no way to drop the
# passive credentials short of deleting the whole config entry.
PASSIVE_CLEAR_TOKEN = "-"


def _normalize_hex32(value: str) -> str | None:
    """Accept colons/whitespace in the input; return 32 lowercase hex chars, or None.

    Validated by regex rather than by `bytes.fromhex`, which silently *skips*
    tabs, newlines and other whitespace: a paste out of a wrapped terminal could
    otherwise satisfy a 32-*character* check while decoding to 15 bytes, and a
    wrong-length key raises in the coordinator's HA-side advertisement callback.
    """
    cleaned = re.sub(r"[\s:]", "", value).lower()
    if not _HEX32_RE.fullmatch(cleaned):
        return None
    return cleaned


def _check_passive_pair(
    passive_key: str | None, passive_iv: str | None, errors: dict[str, str]
) -> None:
    """Reject a half-filled passive pair.

    Both halves are needed for passive_enabled to be true, so storing only one
    would leave the feature silently off with nothing in the UI to explain why.
    The clear token counts as "not set" here: entering it in one field means
    "remove the pair", which is handled by the caller.

    Fields that already have a more specific error are left alone — a malformed
    value comes back from the normalizer as None (indistinguishable from blank),
    and telling a user who filled the other field correctly to "fill in both"
    would bury the "must be 32 hex chars" message on the field that is actually
    wrong.
    """
    if CONF_PASSIVE_KEY in errors or CONF_PASSIVE_IV in errors:
        return
    key_set = passive_key is not None and passive_key != PASSIVE_CLEAR_TOKEN
    iv_set = passive_iv is not None and passive_iv != PASSIVE_CLEAR_TOKEN
    if key_set != iv_set:
        errors[CONF_PASSIVE_KEY] = "passive_pair_incomplete"
        errors[CONF_PASSIVE_IV] = "passive_pair_incomplete"


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
            passive_key = _normalize_optional_hex32(
                user_input.get(CONF_PASSIVE_KEY, ""), errors, CONF_PASSIVE_KEY
            )
            passive_iv = _normalize_optional_hex32(
                user_input.get(CONF_PASSIVE_IV, ""), errors, CONF_PASSIVE_IV
            )
            _check_passive_pair(passive_key, passive_iv, errors)
            if not errors:
                assert self._discovered_address is not None
                # Store credentials in entry data (durable, not exposed
                # to options). Protocol selection goes into options
                # because the user can change it later — start with
                # whatever they picked at setup.
                proto_choice = user_input.get(CONF_METER_PROTOCOL, METER_PROTOCOL_UNCHANGED)
                data: dict[str, Any] = {
                    CONF_ADDRESS: self._discovered_address,
                    CONF_KEY: key,
                    CONF_IV: iv,
                }
                # Absent passive keys are simply "not configured": the
                # coordinator then never registers for advertisements. (At
                # setup there is nothing to clear, so the token is ignored.)
                if passive_key and passive_key != PASSIVE_CLEAR_TOKEN:
                    data[CONF_PASSIVE_KEY] = passive_key
                if passive_iv and passive_iv != PASSIVE_CLEAR_TOKEN:
                    data[CONF_PASSIVE_IV] = passive_iv
                return self.async_create_entry(
                    title=self._discovered_name or self._discovered_address,
                    data=data,
                    options={
                        CONF_METER_PROTOCOL: proto_choice,
                        CONF_PROSUMER: user_input.get(CONF_PROSUMER, False),
                    },
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
            passive_key = _normalize_optional_hex32(
                user_input.get(CONF_PASSIVE_KEY, ""), errors, CONF_PASSIVE_KEY
            )
            passive_iv = _normalize_optional_hex32(
                user_input.get(CONF_PASSIVE_IV, ""), errors, CONF_PASSIVE_IV
            )
            _check_passive_pair(passive_key, passive_iv, errors)
            if not errors:
                assert self._reauth_entry is not None
                new_data = {**self._reauth_entry.data, CONF_KEY: key, CONF_IV: iv}
                # Blank passive fields keep whatever was already stored (a
                # reauth prompted by the GATT key must not quietly drop them);
                # the clear token removes the pair outright.
                if PASSIVE_CLEAR_TOKEN in (passive_key, passive_iv):
                    new_data.pop(CONF_PASSIVE_KEY, None)
                    new_data.pop(CONF_PASSIVE_IV, None)
                else:
                    if passive_key:
                        new_data[CONF_PASSIVE_KEY] = passive_key
                    if passive_iv:
                        new_data[CONF_PASSIVE_IV] = passive_iv
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
                        CONF_PROSUMER: user_input.get(CONF_PROSUMER, False),
                        CONF_PASSIVE: user_input.get(CONF_PASSIVE, DEFAULT_PASSIVE),
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
                    vol.Required(
                        CONF_PROSUMER,
                        default=opts.get(CONF_PROSUMER, False),
                    ): bool,
                    vol.Required(
                        CONF_PASSIVE,
                        default=opts.get(CONF_PASSIVE, DEFAULT_PASSIVE),
                    ): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "min_s": str(MIN_POLL_INTERVAL_S),
                "max_s": str(MAX_POLL_INTERVAL_S),
            },
        )
