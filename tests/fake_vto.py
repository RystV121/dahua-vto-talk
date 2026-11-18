"""A simulated Dahua VTO2202F for tests.

Behaviour copied from what the real door station did on 2026-11-18:

* RTSP Digest (realm "Login to <id>", no qop); the nonce is only accepted
  on the connection it was issued on.
* DESCRIBE without ``Require: www.onvif.org/ver20/backchannel`` lists no
  talk track; with it, the talk track is ``trackID=5`` with ``a=sendonly``
  in the same format as the main stream's audio.
* Content-Base ends in ``...proto=Onvif/`` (slash after the query string).
* Main stream audio starts as PCM 16 kHz (-> ``97 L16/16000``) and can be
  switched to G.711A 8 kHz (-> ``8 PCMA/8000``) with configManager setConfig.
  Optionally it reverts on its own after ``revert_after`` seconds, like the
  real one did within two minutes.
* Only one client may hold the talk track.
* The "speaker" only reproduces G.711 at 8 kHz; L16/16000 connects but stays
  silent (rroller/dahua: "With PCM (L16) at 16 kHz the call connected and
  nothing was heard at the door").
* HTTP CGI with Digest qop=auth: getConfig/setConfig Encode, magicBox,
  audio.cgi postAudio.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import struct
from dataclasses import dataclass, field

from aiohttp import web

from custom_components.dahua_vto_talk.vto.codecs import alaw_to_linear, ulaw_to_linear

SERIAL = "1A2B3C4D5E6F7G8"


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


ENCODE_TEMPLATE = """table.Encode[0].ExtraFormat[0].Audio.Bitrate=64
table.Encode[0].ExtraFormat[0].Audio.Compression=PCM
table.Encode[0].ExtraFormat[0].Audio.Depth=16
table.Encode[0].ExtraFormat[0].Audio.Frequency=16000
table.Encode[0].ExtraFormat[0].AudioEnable=true
table.Encode[0].MainFormat[0].Audio.Bitrate=64
table.Encode[0].MainFormat[0].Audio.Compression={comp}
table.Encode[0].MainFormat[0].Audio.Depth=16
table.Encode[0].MainFormat[0].Audio.Frequency={freq}
table.Encode[0].MainFormat[0].Audio.Mode=0
table.Encode[0].MainFormat[0].Audio.Pack=DHAV
table.Encode[0].MainFormat[0].AudioEnable=true
table.Encode[0].MainFormat[0].Video.Compression=H.264
table.Encode[0].MainFormat[0].Video.Height=720
table.Encode[0].MainFormat[0].Video.Width=1280
table.Encode[0].MainFormat[1].Audio.Compression=PCM
table.Encode[0].MainFormat[1].Audio.Frequency=16000
"""


@dataclass
class ReceivedTalk:
    payload_type: int
    packets: list[tuple[int, int, int, bytes]] = field(default_factory=list)  # seq, ts, marker, payload


class FakeVto:
    def __init__(
        self,
        username: str = "admin",
        password: str = "T3st@pass",
        revert_after: float | None = None,
        l16_audible: bool = False,
        rtsp_port: int = 0,
        http_port: int = 0,
        host: str = "127.0.0.1",
    ) -> None:
        self._want_rtsp_port = rtsp_port
        self._want_http_port = http_port
        self.bind_host = host
        self.username = username
        self.password = password
        self.compression = "PCM"
        self.frequency = 16000
        self.revert_after = revert_after
        self.l16_audible = l16_audible
        self.realm = "Login to 0123456789abcdef0123456789abcdef"
        self.rtsp_server: asyncio.base_events.Server | None = None
        self.http_runner: web.AppRunner | None = None
        self.rtsp_port = 0
        self.http_port = 0
        self.talk_holder: object | None = None
        self.talks: list[ReceivedTalk] = []
        self.set_config_calls = 0
        self.describe_count = 0
        self.post_audio: list[tuple[str, bytes]] = []
        self._revert_task: asyncio.Task | None = None
        self.log: list[str] = []
        self._http_nonces: set[str] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.rtsp_server = await asyncio.start_server(self._handle_rtsp, self.bind_host, self._want_rtsp_port)
        self.rtsp_port = self.rtsp_server.sockets[0].getsockname()[1]
        app = web.Application()
        app.router.add_route("*", "/cgi-bin/{name}", self._handle_http)
        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, self.bind_host, self._want_http_port)
        await site.start()
        self.http_port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    async def stop(self) -> None:
        if self._revert_task:
            self._revert_task.cancel()
        if self.rtsp_server:
            self.rtsp_server.close()
            for writer in list(self._writers):  # like pulling the power cable
                writer.transport.abort()
            await self.rtsp_server.wait_closed()
        if self.http_runner:
            await self.http_runner.cleanup()

    # --------------------------------------------------------------- codec

    @property
    def backchannel_rtpmap(self) -> tuple[int, str]:
        if self.compression == "G.711A":
            return 8, f"PCMA/{self.frequency}"
        if self.compression == "G.711Mu":
            return 0, f"PCMU/{self.frequency}"
        return 97, f"L16/{self.frequency}"

    def set_codec(self, compression: str, frequency: int) -> None:
        self.compression = compression
        self.frequency = frequency
        if self.revert_after is not None:
            if self._revert_task:
                self._revert_task.cancel()
            self._revert_task = asyncio.get_running_loop().create_task(self._revert())

    async def _revert(self) -> None:
        await asyncio.sleep(self.revert_after or 0)
        self.log.append("codec reverted to PCM/16000")
        self.compression = "PCM"
        self.frequency = 16000

    # ------------------------------------------------------------ speaker

    def heard_pcm(self, talk: ReceivedTalk | None = None) -> list[int]:
        """What the door speaker played, as 8 kHz samples (empty if nothing)."""
        talk = talk or (self.talks[-1] if self.talks else None)
        if talk is None:
            return []
        out: list[int] = []
        for _seq, _ts, _m, payload in talk.packets:
            if talk.payload_type == 8:
                out += [alaw_to_linear(b) for b in payload]
            elif talk.payload_type == 0:
                out += [ulaw_to_linear(b) for b in payload]
            elif self.l16_audible:
                out += [struct.unpack("!h", payload[i : i + 2])[0] for i in range(0, len(payload) - 1, 2)]
        return out

    # ---------------------------------------------------------------- RTSP

    async def _handle_rtsp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonce = os.urandom(16).hex()
        session = str(int.from_bytes(os.urandom(4), "big"))
        content_base = None
        setups: dict[int, str] = {}  # channel -> track
        playing = False
        talk: ReceivedTalk | None = None
        conn_token = object()
        self._writers.add(writer)
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    head = await reader.readexactly(3)
                    channel = head[0]
                    size = struct.unpack("!H", head[1:])[0]
                    data = await reader.readexactly(size)
                    if setups.get(channel) == "trackID=5" and playing and talk is not None:
                        b0, b1, seq, ts, _ssrc = struct.unpack("!BBHII", data[:12])
                        talk.packets.append((seq, ts, b1 >> 7, data[12:]))
                    continue
                raw = first + await reader.readuntil(b"\r\n\r\n")
                text = raw.decode()
                lines = text.split("\r\n")
                method, url, _ = lines[0].split(" ", 2)
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                cseq = headers.get("cseq", "0")
                self.log.append(f"{method} {url}")

                def reply(status: str, extra: dict | None = None, body: bytes = b"") -> None:
                    out = [f"RTSP/1.0 {status}", f"CSeq: {cseq}"]
                    for k, v in (extra or {}).items():
                        out.append(f"{k}: {v}")
                    if body:
                        out.append(f"Content-Length: {len(body)}")
                    writer.write(("\r\n".join(out) + "\r\n\r\n").encode() + body)

                if method in ("DESCRIBE", "SETUP", "PLAY", "TEARDOWN"):
                    if not self._rtsp_auth_ok(headers.get("authorization", ""), method, url, nonce):
                        reply("401 Unauthorized", {"WWW-Authenticate": f'Digest realm="{self.realm}", nonce="{nonce}"'})
                        await writer.drain()
                        continue

                if method == "OPTIONS":
                    reply("200 OK", {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"})
                elif method == "DESCRIBE":
                    self.describe_count += 1
                    backchannel = "www.onvif.org/ver20/backchannel" in headers.get("require", "")
                    path = url.split("://", 1)[1].split("/", 1)[1]
                    content_base = f"rtsp://127.0.0.1:{self.rtsp_port}/{path}/"
                    body = self._sdp(backchannel).encode()
                    reply(
                        "200 OK",
                        {"Content-Base": content_base, "Content-Type": "application/sdp", "Cache-Control": "must-revalidate"},
                        body,
                    )
                elif method == "SETUP":
                    if content_base is None or not url.startswith(content_base):
                        reply("404 Not Found")
                    else:
                        track = url[len(content_base) :]
                        m = re.search(r"interleaved=(\d+)-(\d+)", headers.get("transport", ""))
                        if not m or track not in ("trackID=0", "trackID=1", "trackID=4", "trackID=5"):
                            reply("461 Unsupported Transport")
                        elif track == "trackID=5" and self.talk_holder not in (None, conn_token):
                            reply("503 Service Unavailable")
                        else:
                            if track == "trackID=5":
                                self.talk_holder = conn_token
                                pt, _ = self.backchannel_rtpmap
                                talk = ReceivedTalk(pt)
                                self.talks.append(talk)
                            setups[int(m.group(1))] = track
                            reply(
                                "200 OK",
                                {
                                    "Session": f"{session};timeout=60",
                                    "Transport": f"RTP/AVP/TCP;unicast;interleaved={m.group(1)}-{m.group(2)};ssrc=7CB694B4",
                                },
                            )
                elif method == "PLAY":
                    if headers.get("session", "").split(";")[0] != session or not setups:
                        reply("454 Session Not Found")
                    else:
                        playing = True
                        reply("200 OK", {"Session": session, "Range": "npt=0.000-"})
                        for ch, track in setups.items():
                            if track == "trackID=1":
                                # a little door audio, as the real one streams it
                                pkt = struct.pack("!BBHII", 0x80, 97, 1, 0, 1) + b"\x00" * 64
                                writer.write(b"$" + bytes([ch]) + struct.pack("!H", len(pkt)) + pkt)
                elif method == "TEARDOWN":
                    reply("200 OK", {"Session": session})
                    await writer.drain()
                    break
                else:
                    reply("501 Not Implemented")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            if self.talk_holder is conn_token:
                self.talk_holder = None
            writer.close()

    def _rtsp_auth_ok(self, header: str, method: str, uri: str, nonce: str) -> bool:
        if not header.lower().startswith("digest"):
            return False
        params = dict(re.findall(r'(\w+)="?([^",]*)"?', header))
        if params.get("nonce") != nonce or params.get("uri") != uri or params.get("username") != self.username:
            return False
        ha1 = md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = md5(f"{method}:{uri}")
        return params.get("response") == md5(f"{ha1}:{nonce}:{ha2}")

    def _sdp(self, backchannel: bool) -> str:
        pt, rtpmap = self.backchannel_rtpmap
        lines = [
            "v=0",
            "o=- 2251940710 2251940710 IN IP4 0.0.0.0",
            "s=Media Server",
            "c=IN IP4 0.0.0.0",
            "t=0 0",
            "a=control:*",
            "a=packetization-supported:DH",
            "a=rtppayload-supported:DH",
            "a=range:npt=now-",
            "m=video 0 RTP/AVP 96",
            "a=control:trackID=0",
            "a=framerate:30.000000",
            "a=rtpmap:96 H264/90000",
            "a=recvonly",
            f"m=audio 0 RTP/AVP {pt}",
            "a=control:trackID=1",
            f"a=rtpmap:{pt} {rtpmap}",
            "a=recvonly",
            "m=application 0 RTP/AVP 107",
            "a=control:trackID=4",
            "a=rtpmap:107 vnd.onvif.metadata/90000",
            "a=recvonly",
        ]
        if backchannel:
            lines += [f"m=audio 0 RTP/AVP {pt}", "a=control:trackID=5", f"a=rtpmap:{pt} {rtpmap}", "a=sendonly"]
        return "\r\n".join(lines) + "\r\n"

    # ---------------------------------------------------------------- HTTP

    def _http_auth_ok(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("digest"):
            return False
        params = dict(re.findall(r'(\w+)="?([^",]*)"?', header))
        if params.get("nonce") not in self._http_nonces or params.get("username") != self.username:
            return False
        ha1 = md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = md5(f"{request.method}:{params.get('uri')}")
        expected = md5(f"{ha1}:{params['nonce']}:{params.get('nc')}:{params.get('cnonce')}:{params.get('qop')}:{ha2}")
        return params.get("response") == expected and params.get("uri") == request.raw_path

    async def _handle_http(self, request: web.Request) -> web.StreamResponse:
        if not self._http_auth_ok(request):
            nonce = os.urandom(8).hex()
            self._http_nonces.add(nonce)
            return web.Response(
                status=401,
                headers={"WWW-Authenticate": f'Digest realm="{self.realm}", qop="auth", nonce="{nonce}", opaque="5ccc069c403ebaf9f0171e9517f40e41"'},
            )
        name = request.match_info["name"]
        action = request.query.get("action")
        if name == "configManager.cgi" and action == "getConfig" and request.query.get("name") == "Encode":
            return web.Response(text=ENCODE_TEMPLATE.format(comp=self.compression, freq=self.frequency))
        if name == "configManager.cgi" and action == "setConfig":
            self.set_config_calls += 1
            comp = request.query.get("Encode[0].MainFormat[0].Audio.Compression")
            freq = request.query.get("Encode[0].MainFormat[0].Audio.Frequency")
            if comp and freq:
                self.set_codec(comp, int(freq))
            return web.Response(text="OK\r\n")
        if name == "magicBox.cgi" and action == "getSerialNo":
            return web.Response(text=f"sn={SERIAL}\r\n")
        if name == "magicBox.cgi" and action == "getDeviceType":
            return web.Response(text="type=VTO2202F\r\n")
        if name == "audio.cgi" and action == "postAudio":
            data = await request.read()
            self.post_audio.append((request.headers.get("Content-Type", ""), data))
            return web.Response(text="")
        return web.Response(status=404, text="Error")


def dominant_frequency(samples: list[int], rate: int = 8000, candidates: range = range(200, 2000, 10)) -> float:
    """Goertzel scan: the strongest frequency in the clip."""
    if not samples:
        return 0.0
    n = len(samples)
    best_f, best_p = 0.0, -1.0
    for f in candidates:
        k = int(0.5 + n * f / rate)
        w = 2 * math.pi * k / n
        coeff = 2 * math.cos(w)
        s1 = s2 = 0.0
        for x in samples:
            s0 = x + coeff * s1 - s2
            s2, s1 = s1, s0
        power = s1 * s1 + s2 * s2 - coeff * s1 * s2
        if power > best_p:
            best_p, best_f = power, f
    return best_f


def rms(samples: list[int]) -> float:
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
