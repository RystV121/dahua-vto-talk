"""RTSP client that opens a Dahua VTO's ONVIF audio backchannel and talks on it.

The sequence mirrors what go2rtc does when it plays audio to a Dahua doorbell,
because that is what has been heard working at a real door (rroller/dahua
README, measured on a VTO2211G-WP-S2 and a VTO2311R-WP):

1. ``DESCRIBE`` with ``Require: www.onvif.org/ver20/backchannel`` (the VTO
   only lists the talk track when asked like this), answering the Digest
   challenge on the same TCP connection.
2. ``SETUP`` the ``a=sendonly`` audio track over TCP, interleaved channel =
   2 x its position in the SDP.
3. ``PLAY`` and wait for its 200 before sending anything.
4. RTP, interleaved on the same socket. go2rtc repacks G.711 into 1024-byte
   payloads with the marker set and the timestamp left at zero
   (``pcm.RepackG711(true, ...)``), and that is the default here too.
5. ``OPTIONS`` as keepalive; ``TEARDOWN`` at the end.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

from .digest import DigestAuth, pick_challenge
from .sdp import SdpMedia, find_backchannel, find_receive_audio, parse_sdp

_LOGGER = logging.getLogger(__name__)

BACKCHANNEL_REQUIRE = "www.onvif.org/ver20/backchannel"
USER_AGENT = "HA-DahuaVtoTalk/1.0"
DEFAULT_PATH = "/cam/realmonitor?channel={channel}&subtype={subtype}&unicast=true&proto=Onvif"
REQUEST_TIMEOUT = 8.0


class RtspError(Exception):
    """The door station refused or broke the RTSP conversation."""


class AuthError(RtspError):
    """Wrong user name or password."""


class NoBackchannelError(RtspError):
    """The SDP had no a=sendonly audio track."""


class BackchannelBusyError(RtspError):
    """SETUP of the talk track was refused, usually because someone else holds it."""


@dataclass
class RtspResponse:
    status: int
    reason: str
    headers: dict[str, list[str]] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str, default: str = "") -> str:
        values = self.headers.get(name.lower())
        return values[0] if values else default


def build_url(host: str, port: int, channel: int = 1, subtype: int = 0) -> str:
    return f"rtsp://{host}:{port}" + DEFAULT_PATH.format(channel=channel, subtype=subtype)


def join_control(base: str, control: str) -> str:
    """Track URL the way go2rtc builds it (issue #1236 prefix check)."""
    if "://" in control:
        return control
    if not base.endswith("/") and not control.startswith("/"):
        base += "/"
    return base + control


class BackchannelSession:
    """One RTSP session that holds the VTO's talk channel open."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        channel: int = 1,
        subtype: int = 0,
        url: str | None = None,
        setup_receive_audio: bool = False,
        zero_timestamp: bool = True,
        on_closed: Callable[[str | None], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.url = url or build_url(host, port, channel, subtype)
        self.setup_receive_audio = setup_receive_audio
        self.zero_timestamp = zero_timestamp
        self._on_closed = on_closed

        self.sdp: str = ""
        self.medias: list[SdpMedia] = []
        self.backchannel: SdpMedia | None = None
        self.base_url = self.url
        self.session_id = ""
        self.keepalive_s = 25.0
        self.channel = 0

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = 0
        self._auth: DigestAuth | None = None
        self._tasks: list[asyncio.Task] = []
        self._closed = asyncio.Event()
        self._close_reason: str | None = None
        self._seq = int.from_bytes(os.urandom(2), "big")
        self._ts = 0
        self._ssrc = int.from_bytes(os.urandom(4), "big")
        self.packets_sent = 0
        self.bytes_sent = 0
        self.bytes_received = 0

    # ------------------------------------------------------------------ API

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._closed.is_set()

    @property
    def close_reason(self) -> str | None:
        return self._close_reason

    async def describe_only(self) -> list[SdpMedia]:
        """Connect, DESCRIBE with the backchannel requirement, disconnect."""
        try:
            await self._connect()
            await self._describe()
            return self.medias
        finally:
            await self._hard_close()

    async def open(self) -> SdpMedia:
        """Run DESCRIBE/SETUP/PLAY; return the backchannel media."""
        try:
            await self._connect()
            await self._describe()
            backchannel = find_backchannel(self.medias)
            if backchannel is None:
                raise NoBackchannelError(
                    "the door station offered no talk track (a=sendonly audio) in its RTSP description"
                )
            self.backchannel = backchannel
            if self.setup_receive_audio:
                receive = find_receive_audio(self.medias)
                if receive is not None:
                    await self._setup(receive)
            self.channel = await self._setup(backchannel, backchannel=True)
            await self._play()
        except BaseException:
            await self._hard_close()
            raise
        self._tasks.append(asyncio.create_task(self._read_loop(), name="vto-rtsp-reader"))
        self._tasks.append(asyncio.create_task(self._keepalive_loop(), name="vto-rtsp-keepalive"))
        _LOGGER.debug(
            "Backchannel open on %s: %s, channel %s, session %s",
            self.host,
            backchannel.describe(),
            self.channel,
            self.session_id,
        )
        return backchannel

    def send_payload(self, payload: bytes, timestamp_step: int) -> None:
        """Send one RTP packet on the talk channel. Raises if closed."""
        if not self.is_open or self.backchannel is None:
            raise RtspError(self._close_reason or "backchannel is not open")
        header = struct.pack(
            "!BBHII",
            0x80,
            0x80 | (self.backchannel.payload_type & 0x7F),
            self._seq & 0xFFFF,
            0 if self.zero_timestamp else self._ts & 0xFFFFFFFF,
            self._ssrc,
        )
        packet = header + payload
        assert self._writer is not None
        self._writer.write(b"$" + bytes([self.channel]) + struct.pack("!H", len(packet)) + packet)
        self._seq += 1
        self._ts += timestamp_step
        self.packets_sent += 1
        self.bytes_sent += len(payload)

    async def drain(self) -> None:
        if self._writer is not None:
            await self._writer.drain()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        """TEARDOWN politely and close the socket."""
        if self._writer is not None and not self._closed.is_set():
            with contextlib.suppress(Exception):
                self._send_request("TEARDOWN", self.base_url, {})
                await asyncio.wait_for(self._writer.drain(), 2)
        await self._hard_close()

    # ------------------------------------------------------------ internals

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), REQUEST_TIMEOUT
        )
        self._closed.clear()

    async def _describe(self) -> None:
        response = await self._request(
            "DESCRIBE",
            self.url,
            {"Accept": "application/sdp", "Require": BACKCHANNEL_REQUIRE},
        )
        base = response.header("content-base") or response.header("content-location")
        if base:
            self.base_url = base
        self.sdp = response.body.decode("utf-8", errors="replace")
        self.medias = parse_sdp(self.sdp)

    async def _setup(self, media: SdpMedia, backchannel: bool = False) -> int:
        channel = media.index * 2
        track_url = join_control(self.base_url, media.control)
        try:
            response = await self._request(
                "SETUP",
                track_url,
                {"Transport": f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"},
            )
        except AuthError:
            raise
        except RtspError as err:
            if backchannel:
                raise BackchannelBusyError(
                    f"the door station refused the talk track ({err}); is another app or go2rtc holding it?"
                ) from err
            raise
        session = response.header("session")
        if session and not self.session_id:
            sid, _, rest = session.partition(";")
            self.session_id = sid.strip()
            match = re.search(r"timeout=(\d+)", rest)
            if match:
                timeout = int(match.group(1))
                self.keepalive_s = float(max(5, timeout - 5)) if timeout > 5 else 25.0
        transport = response.header("transport")
        match = re.search(r"interleaved=(\d+)", transport)
        return int(match.group(1)) if match else channel

    async def _play(self) -> None:
        await self._request("PLAY", self.base_url, {"Range": "npt=0.000-"})

    def _send_request(self, method: str, url: str, headers: dict[str, str]) -> None:
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}", f"User-Agent: {USER_AGENT}"]
        if self.session_id and method not in ("DESCRIBE",):
            lines.append(f"Session: {self.session_id}")
        if self._auth is not None:
            lines.append("Authorization: " + self._auth.authorization(method, url))
        lines += [f"{k}: {v}" for k, v in headers.items()]
        assert self._writer is not None
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

    async def _request(self, method: str, url: str, headers: dict[str, str]) -> RtspResponse:
        for attempt in range(2):
            self._send_request(method, url, headers)
            assert self._writer is not None
            await self._writer.drain()
            response = await asyncio.wait_for(self._read_response(), REQUEST_TIMEOUT)
            if response.status == 401:
                challenge = pick_challenge(response.headers.get("www-authenticate", []))
                if attempt == 0 and challenge and self.username:
                    self._auth = DigestAuth.from_header(self.username, self.password, challenge)
                    continue
                raise AuthError(f"{method} was refused with 401: wrong user name or password")
            if response.status != 200:
                raise RtspError(f"{method} answered {response.status} {response.reason}".strip())
            return response
        raise AuthError(f"{method}: unauthorized")

    async def _read_response(self) -> RtspResponse:
        """Read one RTSP response, skipping any interleaved data before it."""
        assert self._reader is not None
        while True:
            first = await self._reader.readexactly(1)
            if first == b"$":
                head = await self._reader.readexactly(3)
                size = struct.unpack("!H", head[1:])[0]
                await self._reader.readexactly(size)
                self.bytes_received += size
                continue
            raw = first + await self._reader.readuntil(b"\r\n\r\n")
            break
        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        status_line = lines[0].split(" ", 2)
        if not status_line[0].startswith("RTSP/"):
            # A request from the server (e.g. OPTIONS); not expected here.
            raise RtspError(f"unexpected message from server: {lines[0]!r}")
        status = int(status_line[1])
        reason = status_line[2] if len(status_line) > 2 else ""
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers.setdefault(key.strip().lower(), []).append(value.strip())
        length = int(headers.get("content-length", ["0"])[0] or 0)
        body = await self._reader.readexactly(length) if length else b""
        return RtspResponse(status, reason, headers, body)

    async def _read_loop(self) -> None:
        """Drain everything the server sends while we talk."""
        reason: str | None = None
        assert self._reader is not None
        try:
            while True:
                first = await self._reader.readexactly(1)
                if first == b"$":
                    head = await self._reader.readexactly(3)
                    size = struct.unpack("!H", head[1:])[0]
                    await self._reader.readexactly(size)
                    self.bytes_received += size
                    continue
                raw = first + await self._reader.readuntil(b"\r\n\r\n")
                text = raw.decode("utf-8", errors="replace")
                match = re.search(r"content-length:\s*(\d+)", text, re.IGNORECASE)
                if match:
                    await self._reader.readexactly(int(match.group(1)))
                status = text.split(" ", 2)
                if text.startswith("RTSP/") and len(status) > 1 and status[1] not in ("200",):
                    _LOGGER.debug("RTSP keepalive answer: %s", text.splitlines()[0])
                if not text.startswith("RTSP/"):
                    # The server sent us a request (some send OPTIONS); answer 200.
                    cseq = re.search(r"cseq:\s*(\d+)", text, re.IGNORECASE)
                    if self._writer is not None:
                        self._writer.write(
                            f"RTSP/1.0 200 OK\r\nCSeq: {cseq.group(1) if cseq else 0}\r\n\r\n".encode()
                        )
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as err:
            reason = f"the door station closed the connection ({type(err).__name__})"
        except Exception as err:  # noqa: BLE001
            reason = f"RTSP read error: {err}"
        self._mark_closed(reason)

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed.is_set():
                await asyncio.sleep(self.keepalive_s)
                if self._writer is None or self._closed.is_set():
                    return
                self._send_request("OPTIONS", self.base_url, {})
                await self._writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._mark_closed(f"keepalive failed: {err}")

    def _mark_closed(self, reason: str | None) -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        if self._writer is not None:
            self._writer.close()
        if self._on_closed is not None:
            try:
                self._on_closed(reason)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("on_closed callback failed")

    async def _hard_close(self) -> None:
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        for task in self._tasks:
            if task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tasks.clear()
        writer = self._writer
        self._mark_closed(self._close_reason)
        self._writer = None
        self._reader = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)


def quote_userinfo(value: str) -> str:
    """Escape a user name or password for an rtsp:// URL (e.g. @ -> %40)."""
    return quote(value, safe="")
