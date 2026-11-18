"""The few Dahua CGI calls this integration needs, over aiohttp with Digest."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from urllib.parse import quote

import aiohttp
from yarl import URL

from .digest import DigestAuth, pick_challenge

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=10)

# Encoder settings that make the VTO offer a G.711A talk track (rroller/dahua:
# "Set the main stream's audio to G.711A at 8 kHz ... With PCM (L16) at 16 kHz
# the call connected and nothing was heard at the door").
WANTED_COMPRESSION = "G.711A"
WANTED_FREQUENCY = "8000"


class DahuaHttpError(Exception):
    """A CGI call failed."""


class DahuaAuthError(DahuaHttpError):
    """Wrong user name or password (or the account is locked)."""


def parse_table(text: str) -> dict[str, str]:
    """``table.Encode[0].MainFormat[0].Audio.Compression=PCM`` -> dict."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def audio_key(fmt: str, index: int, channel_index: int, field: str) -> str:
    return f"table.Encode[{channel_index}].{fmt}[{index}].Audio.{field}"


class DahuaHttp:
    """Minimal CGI client. One instance per door station."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        https: bool = False,
    ) -> None:
        self._session = session
        self._base = f"{'https' if https else 'http'}://{host}:{port}"
        self._username = username
        self._password = password
        self._auth: DigestAuth | None = None
        self._lock = asyncio.Lock()

    async def _request(self, method: str, path: str, **kwargs) -> tuple[int, str]:
        url = self._base + path
        for attempt in range(2):
            headers = dict(kwargs.pop("headers", {}) or {})
            if self._auth is not None:
                headers["Authorization"] = self._auth.authorization(method, path)
            try:
                # encoded=True: send the path exactly as signed. Dahua compares
                # the Digest uri with the raw request line, and the brackets
                # in setConfig keys must stay as they are.
                async with self._session.request(
                    method, URL(url, encoded=True), headers=headers, timeout=TIMEOUT, ssl=False, **kwargs
                ) as response:
                    text = await response.text(errors="replace")
                    if response.status == 401:
                        challenge = pick_challenge(response.headers.getall("WWW-Authenticate", []))
                        if attempt == 0 and challenge:
                            self._auth = DigestAuth.from_header(self._username, self._password, challenge)
                            kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                            continue
                        raise DahuaAuthError("the door station refused the user name or password")
                    return response.status, text
            except (aiohttp.ClientError, TimeoutError) as err:
                raise DahuaHttpError(f"{method} {path}: {err}") from err
        raise DahuaAuthError("unauthorized")

    async def get(self, path: str) -> str:
        status, text = await self._request("GET", path)
        if status != 200:
            raise DahuaHttpError(f"GET {path} answered {status}: {text.strip()[:80]}")
        if text.strip().lower().startswith("error"):
            raise DahuaHttpError(f"GET {path} answered: {text.strip()[:80]}")
        return text

    async def get_config(self, name: str) -> dict[str, str]:
        return parse_table(await self.get(f"/cgi-bin/configManager.cgi?action=getConfig&name={name}"))

    async def set_config(self, values: dict[str, str]) -> None:
        query = "&".join(f"{k}={quote(str(v), safe='.[]')}" for k, v in values.items())
        text = await self.get(f"/cgi-bin/configManager.cgi?action=setConfig&{query}")
        if "ok" not in text.strip().lower():
            raise DahuaHttpError(f"setConfig did not answer OK: {text.strip()[:80]}")

    async def get_serial(self) -> str | None:
        try:
            text = await self.get("/cgi-bin/magicBox.cgi?action=getSerialNo")
        except DahuaAuthError:
            raise
        except DahuaHttpError:
            return None
        return parse_table(text).get("sn") or None

    async def get_device_type(self) -> str | None:
        try:
            text = await self.get("/cgi-bin/magicBox.cgi?action=getDeviceType")
        except DahuaAuthError:
            raise
        except DahuaHttpError:
            return None
        return parse_table(text).get("type") or None

    # ----------------------------------------------------------- audio codec

    async def read_audio_codec(self, channel_index: int = 0, fmt: str = "MainFormat", index: int = 0) -> tuple[str, str]:
        table = await self.get_config("Encode")
        return (
            table.get(audio_key(fmt, index, channel_index, "Compression"), "?"),
            table.get(audio_key(fmt, index, channel_index, "Frequency"), "?"),
        )

    async def write_audio_codec(
        self,
        compression: str = WANTED_COMPRESSION,
        frequency: str = WANTED_FREQUENCY,
        channel_index: int = 0,
        targets: tuple[tuple[str, int], ...] = (("MainFormat", 0),),
    ) -> None:
        values: dict[str, str] = {}
        for fmt, index in targets:
            values[f"Encode[{channel_index}].{fmt}[{index}].Audio.Compression"] = compression
            values[f"Encode[{channel_index}].{fmt}[{index}].Audio.Frequency"] = frequency
        await self.set_config(values)

    # ------------------------------------------------------------- postAudio

    async def post_audio(self, audio: bytes, content_type: str, byte_rate: int, channel: int = 1) -> None:
        """Push a clip to audio.cgi postAudio, paced at the speaker's speed.

        Content-Length is declared (a chunked body would be played as noise)
        and the clip is sent whole: a push abandoned half way has been seen
        to hang a VTO2000A (rroller/dahua #177). On some firmware
        (VTO2211G-WP-S2) this path accepts everything and plays nothing; the
        RTSP backchannel is the one to use there.
        """
        path = f"/cgi-bin/audio.cgi?action=postAudio&httptype=singlepart&channel={channel}"
        chunk = max(1, byte_rate // 25)

        async def body() -> AsyncIterator[bytes]:
            started = time.monotonic()
            sent = 0
            while sent < len(audio):
                piece = audio[sent : sent + chunk]
                yield piece
                sent += len(piece)
                ahead = sent / byte_rate - (time.monotonic() - started)
                if ahead > 0:
                    await asyncio.sleep(ahead)

        # get a fresh challenge first so the long POST is not wasted on a 401
        if self._auth is None:
            await self.get_device_type()
        headers = {"Content-Type": content_type, "Content-Length": str(len(audio))}
        if self._auth is not None:
            headers["Authorization"] = self._auth.authorization("POST", path)
        timeout = aiohttp.ClientTimeout(total=len(audio) / byte_rate * 3 + 20)
        try:
            async with self._session.post(
                URL(self._base + path, encoded=True), data=body(), headers=headers, timeout=timeout, ssl=False
            ) as response:
                if response.status == 401:
                    raise DahuaAuthError("postAudio refused the credentials")
                text = (await response.text(errors="replace")).strip()
                if text.lower().startswith("error"):
                    raise DahuaHttpError(f"postAudio answered {text[:80]}")
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError):
            # postAudio often sends no response at all and just closes
            _LOGGER.debug("postAudio closed without an answer (normal for Dahua)")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DahuaHttpError(f"postAudio: {err}") from err
