"""HTTP endpoint: the audio the card last sent to the gate, as a WAV file."""

from __future__ import annotations

import io
import wave

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .websocket import _device

LAST_TALK_URL = f"/api/{DOMAIN}/last_talk.wav"


def pcm_to_wav(pcm_le: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm_le)
    return buf.getvalue()


class LastTalkView(HomeAssistantView):
    """GET (authenticated): what the card's microphone last sent to the VTO."""

    url = LAST_TALK_URL
    name = f"api:{DOMAIN}:last_talk"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        device = _device(hass, request.query.get("entry_id"))
        if device is None or not device.last_talk_pcm:
            return web.Response(status=404, text="no recording yet")
        return web.Response(
            body=pcm_to_wav(device.last_talk_pcm, device.last_talk_rate),
            content_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )
