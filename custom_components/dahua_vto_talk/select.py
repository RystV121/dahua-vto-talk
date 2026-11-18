"""Select: which ringtone the card plays when someone rings."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_RINGTONE, RINGTONES
from .device import VtoTalkDevice
from .entity import VtoTalkEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([RingtoneSelect(entry.runtime_data)])


class RingtoneSelect(VtoTalkEntity, SelectEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:music-note"
    _attr_options = RINGTONES

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "ringtone")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self.device.ringtone = last.state if last is not None and last.state in RINGTONES else DEFAULT_RINGTONE

    @property
    def current_option(self) -> str:
        return self.device.ringtone

    async def async_select_option(self, option: str) -> None:
        self.device.ringtone = option
        self.async_write_ha_state()
