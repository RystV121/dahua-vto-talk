"""Base entity."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VERSION
from .device import VtoTalkDevice


class VtoTalkEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: VtoTalkDevice, key: str) -> None:
        self.device = device
        entry = device.entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=f"Domofon – rozmowa ({device.host})",
            manufacturer="Dahua",
            model="VTO (two-way audio)",
            sw_version=VERSION,
            configuration_url=f"http://{device.host}:{device.http_port}",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.device.signal, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
