"""Diagnostic sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import TALK_STATES
from .device import VtoTalkDevice
from .entity import VtoTalkEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    device: VtoTalkDevice = entry.runtime_data
    async_add_entities(
        [
            TalkStatusSensor(device),
            MainCodecSensor(device),
            BackchannelSensor(device),
            CodecRevertsSensor(device),
            SubStreamAudioSensor(device),
        ]
    )


class TalkStatusSensor(VtoTalkEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = TALK_STATES
    _attr_icon = "mdi:account-voice"

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "talk_status")

    @property
    def native_value(self) -> str:
        return self.device.talk_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        handle = self.device.active
        attrs: dict[str, Any] = {"client": self.device.talk_client, "last_error": self.device.last_error}
        if handle is not None:
            attrs.update(codec=repr(handle.encoder), **handle.stats())
        return attrs


class MainCodecSensor(VtoTalkEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:waveform"

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "main_codec")

    @property
    def native_value(self) -> str | None:
        return self.device.main_codec

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        checked = self.device.last_codec_check
        return {"last_check": checked.isoformat() if checked else None}


class BackchannelSensor(VtoTalkEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:microphone-message"

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "backchannel_format")

    @property
    def native_value(self) -> str | None:
        return self.device.backchannel_format


class CodecRevertsSensor(VtoTalkEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:restore-alert"

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "codec_reverts")

    @property
    def native_value(self) -> int:
        return self.device.codec_reverts

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.device.last_revert
        return {"last_revert": last.isoformat() if last else None}


class SubStreamAudioSensor(VtoTalkEntity, SensorEntity):
    """Sound of the sub stream (subtype=1) the WebRTC preview plays."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:volume-high"

    def __init__(self, device: VtoTalkDevice) -> None:
        super().__init__(device, "sub_stream_audio")

    @property
    def native_value(self) -> str | None:
        return self.device.sub_stream_audio
