"""Number: how much louder live speech from the card is played at the door."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_TALK_GAIN_DB
from .device import VtoTalkDevice
from .entity import VtoTalkEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([TalkGainNumber(entry.runtime_data)])


class TalkGainNumber(VtoTalkEntity, RestoreNumber):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:volume-plus"
    _attr_native_min_value = 0
    _attr_native_max_value = 24
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "talk_gain")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        value = last.native_value if last is not None and last.native_value is not None else DEFAULT_TALK_GAIN_DB
        self.device.set_talk_gain(value)

    @property
    def native_value(self) -> float:
        return self.device.talk_gain_db

    async def async_set_native_value(self, value: float) -> None:
        self.device.set_talk_gain(value)
        self.async_write_ha_state()
