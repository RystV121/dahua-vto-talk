"""Config and options flow."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CANCEL_CALL_ENTITY,
    CONF_RING_ENTITY,
    CONF_RING_TIMEOUT,
    DEFAULT_RING_TIMEOUT,
    CONF_CHANNEL,
    CONF_FORCE_CODEC,
    CONF_HTTP_PORT,
    CONF_L16_LITTLE_ENDIAN,
    CONF_PACKET_BYTES,
    CONF_RESTORE_CODEC,
    CONF_FIX_SUB_STREAM,
    CONF_RTSP_PORT,
    CONF_SETUP_RECEIVE_AUDIO,
    CONF_SUBTYPE,
    CONF_WATCHDOG_INTERVAL,
    CONF_ZERO_TIMESTAMP,
    DEFAULT_CHANNEL,
    DEFAULT_HTTP_PORT,
    DEFAULT_PACKET_BYTES,
    DEFAULT_RTSP_PORT,
    DEFAULT_WATCHDOG_INTERVAL,
    DOMAIN,
)
from .vto.http_api import DahuaAuthError, DahuaHttp, DahuaHttpError
from .vto.rtsp import AuthError, BackchannelSession, RtspError
from .vto.sdp import find_backchannel

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_RTSP_PORT, default=defaults.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_HTTP_PORT, default=defaults.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_CHANNEL, default=defaults.get(CONF_CHANNEL, DEFAULT_CHANNEL)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=16)
            ),
        }
    )


async def validate(hass, data: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """Return (errors, info). Info has serial/model/backchannel when it works."""
    errors: dict[str, str] = {}
    info: dict[str, Any] = {}
    session = BackchannelSession(
        data[CONF_HOST], data[CONF_RTSP_PORT], data[CONF_USERNAME], data[CONF_PASSWORD], channel=data[CONF_CHANNEL]
    )
    try:
        medias = await session.describe_only()
    except AuthError:
        errors["base"] = "invalid_auth"
        return errors, info
    except (RtspError, OSError, TimeoutError) as err:
        _LOGGER.debug("RTSP check failed: %s", err)
        errors["base"] = "cannot_connect"
        return errors, info
    back = find_backchannel(medias)
    if back is None:
        errors["base"] = "no_backchannel"
        return errors, info
    info["backchannel"] = back.describe()

    http = DahuaHttp(
        async_get_clientsession(hass, verify_ssl=False),
        data[CONF_HOST],
        data[CONF_HTTP_PORT],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )
    try:
        info["serial"] = await http.get_serial()
        info["model"] = await http.get_device_type()
    except DahuaAuthError:
        errors["base"] = "invalid_auth"
    except DahuaHttpError as err:
        # Talking works without HTTP; only codec keeping needs it.
        _LOGGER.warning("HTTP API of %s not reachable (%s); codec keeping will not work", data[CONF_HOST], err)
    return errors, info


class VtoTalkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a door station."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, info = await validate(self.hass, user_input)
            if not errors:
                unique = info.get("serial") or f"{user_input[CONF_HOST]}:{user_input[CONF_CHANNEL]}"
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                model = info.get("model") or "VTO"
                return self.async_create_entry(title=f"{model} ({user_input[CONF_HOST]})", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _info = await validate(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_user_schema(user_input or dict(entry.data)), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VtoTalkOptionsFlow()


class VtoTalkOptionsFlow(OptionsFlow):
    """Tuning knobs, mostly for experimenting at the door."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_PACKET_BYTES] = int(user_input[CONF_PACKET_BYTES])
            user_input[CONF_SUBTYPE] = int(user_input[CONF_SUBTYPE])
            if not user_input.get(CONF_CANCEL_CALL_ENTITY):
                user_input[CONF_CANCEL_CALL_ENTITY] = ""
            if not user_input.get(CONF_RING_ENTITY):
                user_input[CONF_RING_ENTITY] = ""
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RING_ENTITY,
                    description={"suggested_value": o.get(CONF_RING_ENTITY) or None},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor", "event", "input_boolean", "switch", "sensor"])
                ),
                vol.Required(CONF_RING_TIMEOUT, default=o.get(CONF_RING_TIMEOUT, DEFAULT_RING_TIMEOUT)): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=600)
                ),
                vol.Required(CONF_FORCE_CODEC, default=o.get(CONF_FORCE_CODEC, True)): bool,
                vol.Required(CONF_RESTORE_CODEC, default=o.get(CONF_RESTORE_CODEC, False)): bool,
                vol.Required(CONF_FIX_SUB_STREAM, default=o.get(CONF_FIX_SUB_STREAM, True)): bool,
                vol.Required(
                    CONF_WATCHDOG_INTERVAL, default=o.get(CONF_WATCHDOG_INTERVAL, DEFAULT_WATCHDOG_INTERVAL)
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(
                    CONF_CANCEL_CALL_ENTITY,
                    description={"suggested_value": o.get(CONF_CANCEL_CALL_ENTITY) or None},
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["button", "input_button", "script"])),
                vol.Required(CONF_PACKET_BYTES, default=str(o.get(CONF_PACKET_BYTES, DEFAULT_PACKET_BYTES))): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["1024", "512", "320", "160"], mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_ZERO_TIMESTAMP, default=o.get(CONF_ZERO_TIMESTAMP, True)): bool,
                vol.Required(CONF_L16_LITTLE_ENDIAN, default=o.get(CONF_L16_LITTLE_ENDIAN, False)): bool,
                vol.Required(CONF_SETUP_RECEIVE_AUDIO, default=o.get(CONF_SETUP_RECEIVE_AUDIO, False)): bool,
                vol.Required(CONF_SUBTYPE, default=str(o.get(CONF_SUBTYPE, 0))): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["0", "1"], mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
