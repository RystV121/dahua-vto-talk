"""Buttons: test tone, codec fix, stop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .device import VtoTalkDevice
from .entity import VtoTalkEntity


@dataclass(frozen=True, kw_only=True)
class VtoButtonDescription(ButtonEntityDescription):
    press: Callable[[VtoTalkDevice], Awaitable]


async def _fix(device: VtoTalkDevice) -> None:
    await device.ensure_codec()
    await device.probe_backchannel()


BUTTONS = (
    VtoButtonDescription(key="test_tone", icon="mdi:volume-high", press=lambda d: d.play_test_tone()),
    VtoButtonDescription(
        key="test_tone_http",
        icon="mdi:volume-medium",
        entity_category=EntityCategory.DIAGNOSTIC,
        press=lambda d: d.play_test_tone(transport="http"),
    ),
    VtoButtonDescription(
        key="test_tone_small_packets",
        icon="mdi:volume-low",
        entity_category=EntityCategory.DIAGNOSTIC,
        press=lambda d: d.play_test_tone(packet_bytes=160, zero_timestamp=False),
    ),
    VtoButtonDescription(key="fix_codec", icon="mdi:auto-fix", entity_category=EntityCategory.CONFIG, press=_fix),
    VtoButtonDescription(key="stop_talk", icon="mdi:phone-hangup", press=lambda d: d.stop_talk()),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    device: VtoTalkDevice = entry.runtime_data
    async_add_entities(VtoButton(device, desc) for desc in BUTTONS)


class VtoButton(VtoTalkEntity, ButtonEntity):
    entity_description: VtoButtonDescription

    def __init__(self, device: VtoTalkDevice, description: VtoButtonDescription) -> None:
        super().__init__(device, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.press(self.device)
