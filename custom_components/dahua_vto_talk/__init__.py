"""Dahua VTO Talk: two-way audio to a Dahua door station from Home Assistant.

The picture and the door's sound come from the camera you already have (for
example the Dahua integration). This integration adds the missing half: your
voice to the door, over the VTO's ONVIF RTSP audio backchannel, with the VTO's
audio codec kept at G.711A 8 kHz so the speaker actually plays it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CARD_URL, DOMAIN, VERSION
from .device import VtoTalkDevice
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type VtoTalkConfigEntry = ConfigEntry[VtoTalkDevice]

ATTR_ENTRY = "config_entry_id"

SERVICE_TONE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Optional("seconds", default=3): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=20)),
        vol.Optional("frequency", default=800): vol.All(vol.Coerce(float), vol.Range(min=100, max=3500)),
        vol.Optional("transport", default="rtsp"): vol.In(["rtsp", "http"]),
        vol.Optional("packet_bytes"): vol.All(vol.Coerce(int), vol.In([160, 320, 512, 1024])),
        vol.Optional("zero_timestamp"): cv.boolean,
    }
)
SERVICE_MEDIA_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY): cv.string, vol.Required("media_content_id"): cv.string})
SERVICE_SIMPLE_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY): cv.string})


def get_device(hass: HomeAssistant, entry_id: str | None) -> VtoTalkDevice:
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise ServiceValidationError("Nie znaleziono skonfigurowanego domofonu (Dahua VTO Talk)")
    if len(entries) > 1 and not entry_id:
        raise ServiceValidationError("Masz kilka domofonów: podaj config_entry_id")
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket(hass)
    await _register_card(hass)

    async def tone(call: ServiceCall) -> None:
        device = get_device(hass, call.data.get(ATTR_ENTRY))
        kwargs = {k: call.data[k] for k in ("packet_bytes", "zero_timestamp") if k in call.data}
        await device.play_test_tone(call.data["seconds"], call.data["frequency"], call.data["transport"], **kwargs)

    async def media(call: ServiceCall) -> None:
        await get_device(hass, call.data.get(ATTR_ENTRY)).play_media(call.data["media_content_id"])

    async def fix_codec(call: ServiceCall) -> dict:
        device = get_device(hass, call.data.get(ATTR_ENTRY))
        changed = await device.ensure_codec()
        backchannel = await device.probe_backchannel()
        return {"changed": changed, "main_codec": device.main_codec, "backchannel": backchannel}

    async def stop(call: ServiceCall) -> None:
        await get_device(hass, call.data.get(ATTR_ENTRY)).stop_talk()

    hass.services.async_register(DOMAIN, "play_test_tone", tone, schema=SERVICE_TONE_SCHEMA)
    hass.services.async_register(DOMAIN, "play_media", media, schema=SERVICE_MEDIA_SCHEMA)
    hass.services.async_register(
        DOMAIN, "fix_codec", fix_codec, schema=SERVICE_SIMPLE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, "stop", stop, schema=SERVICE_SIMPLE_SCHEMA)
    return True


async def _register_card(hass: HomeAssistant) -> None:
    http = getattr(hass, "http", None)
    if http is None:
        return
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    path = Path(__file__).parent / "frontend" / "dahua-vto-talk-card.js"
    await http.async_register_static_paths([StaticPathConfig(CARD_URL, str(path), cache_headers=False)])
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

        add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")


async def async_setup_entry(hass: HomeAssistant, entry: VtoTalkConfigEntry) -> bool:
    device = VtoTalkDevice(hass, entry)
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await device.async_start()
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: VtoTalkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VtoTalkConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
