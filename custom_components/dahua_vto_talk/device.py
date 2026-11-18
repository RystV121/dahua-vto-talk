"""Runtime object for one door station: codec keeping and talk sessions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CANCEL_CALL_ENTITY,
    CONF_CHANNEL,
    CONF_FIX_SUB_STREAM,
    CONF_FORCE_CODEC,
    CONF_HTTP_PORT,
    CONF_L16_LITTLE_ENDIAN,
    CONF_PACKET_BYTES,
    CONF_RESTORE_CODEC,
    CONF_RTSP_PORT,
    CONF_SETUP_RECEIVE_AUDIO,
    CONF_SUBTYPE,
    CONF_WATCHDOG_INTERVAL,
    CONF_ZERO_TIMESTAMP,
    DEFAULT_CHANNEL,
    DEFAULT_HTTP_PORT,
    DEFAULT_PACKET_BYTES,
    DEFAULT_RTSP_PORT,
    DEFAULT_WATCHDOG_INTERVAL,
    SIGNAL_UPDATE,
    TALK_CONNECTING,
    TALK_ERROR,
    TALK_IDLE,
    TALK_PLAYING,
    TALK_TALKING,
)
from .vto.codecs import Encoder, pcm16le_to_alaw, resample_pcm16le, tone_pcm16le
from .vto.http_api import (
    WANTED_COMPRESSION,
    WANTED_FREQUENCY,
    DahuaHttp,
    DahuaHttpError,
)
from .vto.rtsp import BackchannelSession, RtspError
from .vto.talker import Talker

_LOGGER = logging.getLogger(__name__)

G711 = ("PCMA", "PCMU")


@dataclass
class TalkHandle:
    """An open talk channel and what feeds it."""

    session: BackchannelSession
    talker: Talker
    task: asyncio.Task
    client: str
    encoder: Encoder
    opened: datetime = field(default_factory=dt_util.utcnow)

    @property
    def sample_rate(self) -> int:
        return self.encoder.clock_rate

    def stats(self) -> dict[str, Any]:
        return {
            "packets_sent": self.session.packets_sent,
            "bytes_sent": self.session.bytes_sent,
            "buffered_ms": self.talker.buffered_ms,
            "dropped_ms": int(self.talker.dropped_bytes / self.encoder.byte_rate * 1000),
            "level": round(self.talker.peak / 32768, 3),
        }


class VtoTalkDevice:
    """Everything this integration does with one VTO."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        data = entry.data
        opts = entry.options
        self.host: str = data[CONF_HOST]
        self.username: str = data[CONF_USERNAME]
        self.password: str = data[CONF_PASSWORD]
        self.rtsp_port: int = data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
        self.http_port: int = data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
        self.channel: int = data.get(CONF_CHANNEL, DEFAULT_CHANNEL)
        self.subtype: int = int(opts.get(CONF_SUBTYPE, 0))
        self.force_codec: bool = opts.get(CONF_FORCE_CODEC, True)
        self.watchdog_interval: int = int(opts.get(CONF_WATCHDOG_INTERVAL, DEFAULT_WATCHDOG_INTERVAL))
        self.packet_bytes: int = int(opts.get(CONF_PACKET_BYTES, DEFAULT_PACKET_BYTES))
        self.zero_timestamp: bool = opts.get(CONF_ZERO_TIMESTAMP, True)
        self.l16_little_endian: bool = opts.get(CONF_L16_LITTLE_ENDIAN, False)
        self.setup_receive_audio: bool = opts.get(CONF_SETUP_RECEIVE_AUDIO, False)
        self.cancel_call_entity: str = opts.get(CONF_CANCEL_CALL_ENTITY, "") or ""
        self.restore_codec: bool = opts.get(CONF_RESTORE_CODEC, False)
        self._original_codec: tuple[str, str] | None = None
        self.fix_sub_stream: bool = opts.get(CONF_FIX_SUB_STREAM, True)
        self.sub_codec: str | None = None
        # what the last card session actually sent (16-bit LE PCM), for the
        # card's "play back what went to the gate" check
        self.last_talk_pcm: bytes = b""
        self.last_talk_rate: int = 8000
        self.last_talk_peak: float = 0.0

        self.http = DahuaHttp(
            async_get_clientsession(hass, verify_ssl=False),
            self.host,
            self.http_port,
            self.username,
            self.password,
        )
        self.keep_codec = True  # the switch entity restores the user's choice

        # state shown by entities
        self.talk_state = TALK_IDLE
        self.talk_client: str | None = None
        self.last_error: str | None = None
        self.main_codec: str | None = None
        self.backchannel_format: str | None = None
        self.codec_reverts = 0
        self.last_revert: datetime | None = None
        self.last_codec_check: datetime | None = None
        self._codec_known_good = False

        self._active: TalkHandle | None = None
        self._lock = asyncio.Lock()
        self._unsubs: list = []

    # ----------------------------------------------------------- lifecycle

    @property
    def signal(self) -> str:
        return SIGNAL_UPDATE.format(self.entry.entry_id)

    @callback
    def _refresh(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    async def async_start(self) -> None:
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._watchdog,
                timedelta(seconds=max(10, self.watchdog_interval)),
                name=f"{self.entry.title} codec watchdog",
            )
        )
        # first check shortly after start, without delaying setup
        self._unsubs.append(async_call_later(self.hass, 2, self._watchdog))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.stop_talk()

    # --------------------------------------------------------------- codec

    async def _watchdog(self, _now=None) -> None:
        if not self.keep_codec or self.restore_codec or self._active is not None or self._lock.locked():
            return
        try:
            await self.ensure_codec()
            await self.ensure_sub_stream_audio()
        except DahuaHttpError as err:
            _LOGGER.debug("Codec watchdog on %s: %s", self.host, err)

    async def ensure_sub_stream_audio(self) -> bool:
        """Keep sub stream sound on and in G.711A so a WebRTC preview has audio.

        The VTO sends PCM 16 kHz on the sub stream by default; browsers (WebRTC)
        only play PCMA/PCMU/Opus, so a go2rtc preview of subtype=1 is silent.
        """
        if not self.fix_sub_stream or self.subtype == 1:
            return False
        table = await self.http.get_config("Encode")
        base = f"Encode[{self.channel - 1}].ExtraFormat[0]"
        comp = table.get(f"table.{base}.Audio.Compression")
        freq = table.get(f"table.{base}.Audio.Frequency")
        enabled = table.get(f"table.{base}.AudioEnable")
        if comp is None:
            return False  # no sub stream on this model
        if comp == WANTED_COMPRESSION and freq == WANTED_FREQUENCY and enabled == "true":
            self.sub_codec = f"{comp}/{freq}"
            return False
        await self.http.set_config(
            {
                f"{base}.AudioEnable": "true",
                f"{base}.Audio.Compression": WANTED_COMPRESSION,
                f"{base}.Audio.Frequency": WANTED_FREQUENCY,
            }
        )
        _LOGGER.info("%s sub stream audio was %s/%s (enabled=%s); set to G.711A/8000", self.host, comp, freq, enabled)
        self.sub_codec = f"{WANTED_COMPRESSION}/{WANTED_FREQUENCY}"
        return True

    @property
    def _encode_format(self) -> tuple[str, int]:
        """Which encoder table feeds the RTSP stream we talk on."""
        return ("ExtraFormat", 0) if self.subtype == 1 else ("MainFormat", 0)

    async def _read_codec_pair(self) -> tuple[str, str]:
        fmt, index = self._encode_format
        return await self.http.read_audio_codec(channel_index=self.channel - 1, fmt=fmt, index=index)

    async def _write_codec_pair(self, comp: str = WANTED_COMPRESSION, freq: str = WANTED_FREQUENCY) -> None:
        await self.http.write_audio_codec(comp, freq, channel_index=self.channel - 1, targets=(self._encode_format,))

    async def read_codec(self) -> str:
        comp, freq = await self._read_codec_pair()
        self.main_codec = f"{comp}/{freq}"
        self.last_codec_check = dt_util.utcnow()
        self._refresh()
        return self.main_codec

    async def ensure_codec(self) -> bool:
        """Make the main stream (and so the talk track) G.711A 8 kHz.

        Returns True if it had to be changed. The VTO2202F has been seen to
        go back to PCM 16 kHz on its own within minutes, so this is checked
        right before every talk session and by the watchdog.
        """
        comp, freq = await self._read_codec_pair()
        self.last_codec_check = dt_util.utcnow()
        if comp == WANTED_COMPRESSION and freq == WANTED_FREQUENCY:
            self.main_codec = f"{comp}/{freq}"
            self._codec_known_good = True
            self._refresh()
            return False
        if self._original_codec is None:
            self._original_codec = (comp, freq)
        if self._codec_known_good:
            self.codec_reverts += 1
            self.last_revert = dt_util.utcnow()
            _LOGGER.info(
                "%s switched its audio back to %s/%s (revert #%s); setting G.711A/8000 again",
                self.host,
                comp,
                freq,
                self.codec_reverts,
            )
        await self._write_codec_pair()
        comp, freq = await self._read_codec_pair()
        self.main_codec = f"{comp}/{freq}"
        self._codec_known_good = comp == WANTED_COMPRESSION and freq == WANTED_FREQUENCY
        self._refresh()
        return True

    async def probe_backchannel(self) -> str:
        """DESCRIBE only: what format would the talk track have right now."""
        session = self._new_session()
        from .vto.sdp import find_backchannel  # noqa: PLC0415

        medias = await session.describe_only()
        back = find_backchannel(medias)
        self.backchannel_format = back.describe() if back else "none"
        self._refresh()
        return self.backchannel_format

    # ---------------------------------------------------------------- talk

    def _new_session(self) -> BackchannelSession:
        return BackchannelSession(
            self.host,
            self.rtsp_port,
            self.username,
            self.password,
            channel=self.channel,
            subtype=self.subtype,
            setup_receive_audio=self.setup_receive_audio,
            zero_timestamp=self.zero_timestamp,
            on_closed=self._on_session_closed,
        )

    @callback
    def _on_session_closed(self, reason: str | None) -> None:
        if reason:
            _LOGGER.warning("Talk channel to %s closed: %s", self.host, reason)
            self.last_error = reason

    def _set_state(self, state: str, client: str | None = None, error: str | None = None) -> None:
        self.talk_state = state
        self.talk_client = client
        if error is not None:
            self.last_error = error
        self._refresh()

    @property
    def active(self) -> TalkHandle | None:
        return self._active

    async def _press_cancel_call(self) -> None:
        entity_id = self.cancel_call_entity
        if not entity_id:
            return
        domain = entity_id.split(".", 1)[0]
        service = {"button": "press", "input_button": "press", "script": "turn_on", "switch": "turn_on"}.get(domain)
        if service is None:
            return
        try:
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)
            # the VTO needs a moment to leave the call before the talk track is free
            await asyncio.sleep(0.5)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not press %s before talking: %s", entity_id, err)

    async def open_talk(self, client: str, *, packet_bytes: int | None = None, zero_timestamp: bool | None = None) -> TalkHandle:
        """Open the talk track and start the sender. Replaces any open one."""
        async with self._lock:
            await self._stop_active()
            self._set_state(TALK_CONNECTING, client)
            try:
                handle = await self._open(client, packet_bytes, zero_timestamp)
            except Exception as err:
                message = _friendly(err)
                self._set_state(TALK_ERROR, None, message)
                raise HomeAssistantError(message) from err
            self._active = handle
            self.last_error = None
            self._set_state(TALK_PLAYING if client != "card" else TALK_TALKING, client)
            handle.task.add_done_callback(lambda _t, h=handle: self.hass.async_create_task(self._on_task_done(h)))
            return handle

    async def _open(self, client: str, packet_bytes: int | None, zero_timestamp: bool | None) -> TalkHandle:
        await self._press_cancel_call()
        session: BackchannelSession | None = None
        for attempt in range(3):
            if self.force_codec:
                try:
                    await self.ensure_codec()
                except DahuaHttpError as err:
                    _LOGGER.warning("Could not check/set the audio codec over HTTP: %s", err)
            session = self._new_session()
            if zero_timestamp is not None:
                session.zero_timestamp = zero_timestamp
            back = await session.open()
            self.backchannel_format = back.describe()
            if back.encoding in G711 or not self.force_codec or attempt == 2:
                break
            # The VTO still offers L16: it can lag behind setConfig. Try again.
            _LOGGER.debug("Talk track is %s, retrying after setting the codec again", back.describe())
            await session.close()
            await asyncio.sleep(0.7)
        assert session is not None and session.backchannel is not None
        back = session.backchannel
        if back.encoding not in G711:
            _LOGGER.warning(
                "%s offers its talk track as %s; door stations of this family usually stay "
                "silent unless it is G.711A/8000. Sending it anyway.",
                self.host,
                back.describe(),
            )
        try:
            encoder = Encoder(back.encoding, back.clock_rate, self.l16_little_endian)
        except ValueError:
            await session.close()
            raise
        talker = Talker(session, encoder, packet_bytes or self.packet_bytes)
        task = self.hass.async_create_background_task(talker.run(), f"{self.host} talk sender")
        return TalkHandle(session, talker, task, client, encoder)

    async def _on_task_done(self, handle: TalkHandle) -> None:
        if handle.talker._finishing and handle.session.close_reason is None:  # noqa: SLF001
            # a clip finished normally; whoever started it closes the channel
            return
        if self._active is handle:
            async with self._lock:
                if self._active is handle:
                    await self._close_handle(handle)
                    self._active = None
                    await self._maybe_restore_codec()
                    reason = handle.session.close_reason
                    self._set_state(TALK_ERROR if reason else TALK_IDLE, None, reason)

    async def _close_handle(self, handle: TalkHandle) -> None:
        if handle.client == "card" and handle.talker.record:
            self.last_talk_pcm = bytes(handle.talker.record)
            self.last_talk_rate = handle.sample_rate
            self.last_talk_peak = round(handle.talker.max_peak / 32768, 3)
        handle.talker.finish()
        if not handle.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(handle.task), 1.5)
            except (TimeoutError, asyncio.CancelledError):
                handle.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await handle.task
            except Exception:  # noqa: BLE001
                pass
        await handle.session.close()

    async def _stop_active(self) -> None:
        handle, self._active = self._active, None
        if handle is not None:
            await self._close_handle(handle)
            await self._maybe_restore_codec()
            self._set_state(TALK_IDLE)

    async def _maybe_restore_codec(self) -> None:
        """Give DMSS its original audio back once Home Assistant stops talking."""
        if not self.restore_codec or self._original_codec is None:
            return
        comp, freq = self._original_codec
        try:
            await self._write_codec_pair(comp, freq)
            self.main_codec = f"{comp}/{freq}"
            self._codec_known_good = False
        except DahuaHttpError as err:
            _LOGGER.warning("Could not restore the audio codec to %s/%s: %s", comp, freq, err)

    async def stop_talk(self, handle: TalkHandle | None = None) -> None:
        async with self._lock:
            if handle is not None and handle is not self._active:
                return
            await self._stop_active()

    async def play_pcm(self, pcm_le: bytes, sample_rate: int, client: str, **kwargs: Any) -> None:
        handle = await self.open_talk(client, **kwargs)
        try:
            pcm = resample_pcm16le(pcm_le, sample_rate, handle.sample_rate)
            handle.talker._buffer += handle.encoder.encode(pcm)  # noqa: SLF001
            handle.talker.finish()
            await asyncio.wait_for(asyncio.shield(handle.task), len(pcm) / 2 / handle.sample_rate + 10)
            await asyncio.sleep(min(1.0, handle.talker.packet_seconds * 2))
        finally:
            await self.stop_talk(handle)

    async def play_test_tone(
        self,
        seconds: float = 3.0,
        frequency: float = 800.0,
        transport: str = "rtsp",
        **kwargs: Any,
    ) -> None:
        if transport == "http":
            pcm = tone_pcm16le(8000, seconds, frequency)
            self._set_state(TALK_PLAYING, "tone_http")
            try:
                await self.http.post_audio(pcm16le_to_alaw(pcm), "Audio/G.711A", 8000, self.channel)
            except DahuaHttpError as err:
                self._set_state(TALK_ERROR, None, str(err))
                raise HomeAssistantError(str(err)) from err
            self._set_state(TALK_IDLE)
            return
        await self.play_pcm(tone_pcm16le(8000, seconds, frequency), 8000, "tone", **kwargs)

    async def play_media(self, media_content_id: str) -> None:
        pcm = await _decode_media(self.hass, media_content_id)
        await self.play_pcm(pcm, 8000, "media")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "rtsp_port": self.rtsp_port,
            "http_port": self.http_port,
            "talk_state": self.talk_state,
            "main_codec": self.main_codec,
            "sub_stream_codec": self.sub_codec,
            "last_talk_seconds": round(len(self.last_talk_pcm) / 2 / self.last_talk_rate, 1),
            "last_talk_peak": self.last_talk_peak,
            "backchannel_format": self.backchannel_format,
            "codec_reverts": self.codec_reverts,
            "last_revert": self.last_revert.isoformat() if self.last_revert else None,
            "last_error": self.last_error,
            "keep_codec": self.keep_codec,
            "restore_codec": self.restore_codec,
            "original_codec": self._original_codec,
            "active": {"client": self._active.client, "codec": repr(self._active.encoder), **self._active.stats()}
            if self._active
            else None,
            "options": dict(self.entry.options),
        }


def _friendly(err: Exception) -> str:
    from .vto.rtsp import AuthError, BackchannelBusyError, NoBackchannelError  # noqa: PLC0415

    if isinstance(err, AuthError):
        return "Domofon odrzucił login/hasło (RTSP 401)."
    if isinstance(err, BackchannelBusyError):
        return (
            "Kanał mówienia jest zajęty. Zamknij DMSS/podgląd z kanałem zwrotnym "
            "(w integracji Dahua włącz 'Don't open the two-way audio channel when streaming')."
        )
    if isinstance(err, NoBackchannelError):
        return "Domofon nie wystawił ścieżki do mówienia (brak a=sendonly w SDP)."
    if isinstance(err, (TimeoutError, OSError)):
        return f"Brak połączenia z domofonem: {err or type(err).__name__}"
    if isinstance(err, RtspError):
        return f"Błąd RTSP: {err}"
    return f"{type(err).__name__}: {err}"


async def _decode_media(hass: HomeAssistant, media_content_id: str) -> bytes:
    """Any media (TTS, file, URL) -> 8 kHz mono s16le via HA's ffmpeg."""
    from homeassistant.components import media_source  # noqa: PLC0415
    from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415
    from homeassistant.components.media_player import async_process_play_media_url  # noqa: PLC0415

    url = media_content_id
    if media_source.is_media_source_id(url):
        resolved = await media_source.async_resolve_media(hass, url, None)
        url = resolved.url
    if url.startswith("/"):
        url = async_process_play_media_url(hass, url)
    if not url.startswith(("http://", "https://")):
        raise HomeAssistantError("Podaj media-source:// albo adres http(s)")
    binary = get_ffmpeg_manager(hass).binary
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        url,
        "-vn",
        "-t",
        "120",
        "-ar",
        "8000",
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 60)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise HomeAssistantError("ffmpeg nie zdążył przekonwertować pliku") from exc
    if proc.returncode != 0 or not out:
        raise HomeAssistantError(f"ffmpeg: {err.decode(errors='replace').strip()[:200] or 'brak dźwięku'}")
    return out
