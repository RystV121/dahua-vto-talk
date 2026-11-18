"""Downloadable diagnostics (Settings -> Devices -> ... -> Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .vto.sdp import find_backchannel


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    device = entry.runtime_data
    info: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), {CONF_PASSWORD, CONF_USERNAME}),
        "device": device.diagnostics(),
    }
    # a fresh look at what the door offers right now
    try:
        info["encode_audio"] = {
            k: v for k, v in (await device.http.get_config("Encode")).items() if ".Audio." in k
        }
    except Exception as err:  # noqa: BLE001
        info["encode_audio"] = f"error: {err}"
    try:
        session = device._new_session()  # noqa: SLF001
        medias = await session.describe_only()
        back = find_backchannel(medias)
        info["rtsp_describe"] = {
            "content_base": session.base_url,
            "medias": [
                {"index": m.index, "kind": m.kind, "control": m.control, "direction": m.direction, "format": m.describe()}
                for m in medias
            ],
            "backchannel": back.describe() if back else None,
            "sdp": session.sdp,
        }
    except Exception as err:  # noqa: BLE001
        info["rtsp_describe"] = f"error: {err}"
    return info
