"""Switch: keep the VTO's audio at G.711A 8 kHz in the background."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .device import VtoTalkDevice
from .entity import VtoTalkEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([KeepCodecSwitch(entry.runtime_data)])


class KeepCodecSwitch(VtoTalkEntity, SwitchEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:shield-sync"

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "keep_codec")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self.device.keep_codec = not (last is not None and last.state == STATE_OFF)

    @property
    def is_on(self) -> bool:
        return self.device.keep_codec

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.device.keep_codec = True
        self.async_write_ha_state()
        await self.device._watchdog()  # noqa: SLF001

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.device.keep_codec = False
        self.async_write_ha_state()
