"""Cross-check the simulator against real go2rtc.

go2rtc is known to make a real Dahua VTO speak. If the real go2rtc binary can
play a clip into the simulated VTO and the simulated speaker hears it, the
simulator speaks the same RTSP as the real door station; and the packets
go2rtc sends are the reference our own sender copies.

Skipped unless GO2RTC_BIN points at a go2rtc binary and ffmpeg is installed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile

import aiohttp
import pytest

from .fake_vto import FakeVto, dominant_frequency

GO2RTC = os.environ.get("GO2RTC_BIN", "")
pytestmark = pytest.mark.skipif(
    not (GO2RTC and os.path.exists(GO2RTC) and shutil.which("ffmpeg")), reason="needs GO2RTC_BIN and ffmpeg"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run_go2rtc_play(vto: FakeVto, seconds: float = 2.0, freq: int = 800) -> str:
    workdir = tempfile.mkdtemp()
    wav = os.path.join(workdir, "tone.wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-ar", "8000", "-ac", "1", wav,
    )
    await proc.wait()
    api = free_port()
    cfg = os.path.join(workdir, "go2rtc.yaml")
    with open(cfg, "w") as f:
        f.write(
            f"""api:
  listen: "127.0.0.1:{api}"
rtsp:
  listen: ""
webrtc:
  listen: ""
log:
  level: debug
streams:
  vto: rtsp://admin:T3st%40pass@127.0.0.1:{vto.rtsp_port}/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif
"""
        )
    go = await asyncio.create_subprocess_exec(
        GO2RTC, "-config", cfg, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(50):
                try:
                    async with http.get(f"http://127.0.0.1:{api}/api") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.1)
            async with http.post(
                f"http://127.0.0.1:{api}/api/streams",
                params={"dst": "vto", "src": f"ffmpeg:{wav}#audio=pcma#input=file"},
            ) as r:
                body = await r.text()
        await asyncio.sleep(seconds + 2)
        return body
    finally:
        go.terminate()
        out, _ = await go.communicate()
        log = out.decode(errors="replace")
        print(log[-3000:])


async def test_go2rtc_is_heard_by_the_simulator_with_pcma() -> None:
    vto = FakeVto()
    await vto.start()
    try:
        vto.set_codec("G.711A", 8000)
        await run_go2rtc_play(vto)
        assert vto.talks, "go2rtc never set up the talk track"
        talk = vto.talks[-1]
        assert talk.payload_type == 8
        sizes = {len(p) for *_x, p in talk.packets}
        stamps = {ts for _s, ts, _m, _p in talk.packets}
        markers = {m for _s, _ts, m, _p in talk.packets}
        print("go2rtc packets:", len(talk.packets), "sizes", sizes, "timestamps", sorted(stamps)[:3], "markers", markers)
        heard = vto.heard_pcm(talk)
        assert len(heard) > 8000
        assert abs(dominant_frequency(heard[:8000]) - 800) <= 10
        # what our sender copies
        assert sizes == {1024}
        assert stamps == {0}
        assert markers == {1}
    finally:
        await vto.stop()


async def test_go2rtc_with_l16_track_is_silent() -> None:
    """The night's symptom, reproduced with real go2rtc: nothing at the door."""
    vto = FakeVto()
    await vto.start()
    try:
        await run_go2rtc_play(vto, seconds=1.0)
        assert vto.heard_pcm() == []
    finally:
        await vto.stop()
