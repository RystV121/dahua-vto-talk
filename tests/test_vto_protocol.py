"""Protocol tests against the simulated VTO (no Home Assistant involved)."""

from __future__ import annotations

import asyncio
import math

import pytest

from custom_components.dahua_vto_talk.vto.codecs import (
    Encoder,
    alaw_to_linear,
    pcm16le_to_alaw,
    pcm16le_to_be,
    tone_pcm16le,
    ulaw_to_linear,
    pcm16le_to_ulaw,
)
from custom_components.dahua_vto_talk.vto.digest import DigestAuth, parse_challenge
from custom_components.dahua_vto_talk.vto.rtsp import (
    AuthError,
    BackchannelBusyError,
    BackchannelSession,
    NoBackchannelError,
    join_control,
)
from custom_components.dahua_vto_talk.vto.sdp import find_backchannel, parse_sdp
from custom_components.dahua_vto_talk.vto.talker import Talker

from .fake_vto import FakeVto, dominant_frequency, rms

# The exact SDP the real VTO2202F sent at 01:38 on 2026-11-18.
REAL_SDP_L16 = """v=0
o=- 2251940710 2251940710 IN IP4 0.0.0.0
s=Media Server
c=IN IP4 0.0.0.0
t=0 0
a=control:*
a=packetization-supported:DH
a=rtppayload-supported:DH
a=range:npt=now-
a=x-packetization-supported:IV
a=x-rtppayload-supported:IV
m=video 0 RTP/AVP 96
a=control:trackID=0
a=framerate:30.000000
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1;profile-level-id=42001F;sprop-parameter-sets=Z0IAH5Y1QKALdNwUBBUAAAMAAQAAAwA8hAA=,aM4xsgA=
a=recvonly
m=audio 0 RTP/AVP 97
a=control:trackID=1
a=rtpmap:97 L16/16000
a=recvonly
m=application 0 RTP/AVP 107
a=control:trackID=4
a=rtpmap:107 vnd.onvif.metadata/90000
a=recvonly
m=audio 0 RTP/AVP 97
a=control:trackID=5
a=rtpmap:97 L16/16000
a=sendonly
"""


# ------------------------------------------------------------------ pure


def test_parse_real_sdp() -> None:
    medias = parse_sdp(REAL_SDP_L16)
    assert [m.kind for m in medias] == ["video", "audio", "application", "audio"]
    back = find_backchannel(medias)
    assert back is not None
    assert back.control == "trackID=5"
    assert back.index == 3  # -> interleaved 6-7, as go2rtc would pick
    assert (back.encoding, back.clock_rate, back.payload_type) == ("L16", 16000, 97)
    pcma = parse_sdp(REAL_SDP_L16.replace("97 L16/16000", "8 PCMA/8000").replace("RTP/AVP 97", "RTP/AVP 8"))
    back = find_backchannel(pcma)
    assert (back.encoding, back.clock_rate, back.payload_type) == ("PCMA", 8000, 8)


def test_static_payload_without_rtpmap() -> None:
    medias = parse_sdp("m=audio 0 RTP/AVP 8\r\na=control:trackID=5\r\na=sendonly\r\n")
    assert (medias[0].encoding, medias[0].clock_rate) == ("PCMA", 8000)


def test_join_control_dahua_content_base() -> None:
    base = "rtsp://192.168.1.108:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif/"
    assert join_control(base, "trackID=5") == base + "trackID=5"
    assert join_control("rtsp://h/x", "trackID=5") == "rtsp://h/x/trackID=5"
    assert join_control("rtsp://h/x", "rtsp://h/y") == "rtsp://h/y"


def test_digest_rfc2617_example() -> None:
    auth = DigestAuth.from_header(
        "Mufasa",
        "Circle Of Life",
        'Digest realm="testrealm@host.com", qop="auth,auth-int", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"',
    )
    header = auth.authorization("GET", "/dir/index.html")
    params = parse_challenge(header)[1]
    # recompute with the cnonce the client chose
    import hashlib

    h = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    ha1 = h("Mufasa:testrealm@host.com:Circle Of Life")
    ha2 = h("GET:/dir/index.html")
    expected = h(f"{ha1}:{params['nonce']}:{params['nc']}:{params['cnonce']}:auth:{ha2}")
    assert params["response"] == expected
    assert params["opaque"] == "5ccc069c403ebaf9f0171e9517f40e41"


def test_digest_without_qop_matches_powershell_probe() -> None:
    # same formula the PowerShell probe used successfully against the real VTO
    auth = DigestAuth.from_header("admin", "pw", 'Digest realm="Login to x", nonce="n1"')
    params = parse_challenge(auth.authorization("DESCRIBE", "rtsp://h/p"))[1]
    import hashlib

    h = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    assert params["response"] == h(f"{h('admin:Login to x:pw')}:n1:{h('DESCRIBE:rtsp://h/p')}")
    assert "qop" not in params


def test_alaw_matches_reference_and_round_trips() -> None:
    # reference points from ITU-T G.711 tables
    assert pcm16le_to_alaw((0).to_bytes(2, "little", signed=True)) == b"\xd5"
    assert pcm16le_to_alaw((-1).to_bytes(2, "little", signed=True)) == b"\x55"
    assert pcm16le_to_alaw((32767).to_bytes(2, "little", signed=True)) == b"\xaa"
    assert pcm16le_to_alaw((-32768).to_bytes(2, "little", signed=True)) == b"\x2a"
    pcm = tone_pcm16le(8000, 0.5, 440, beep_on=1, beep_off=0)
    samples = [int.from_bytes(pcm[i : i + 2], "little", signed=True) for i in range(0, len(pcm), 2)]
    decoded = [alaw_to_linear(b) for b in pcm16le_to_alaw(pcm)]
    noise = math.sqrt(sum((a - b) ** 2 for a, b in zip(samples, decoded, strict=True)) / len(samples))
    snr = 20 * math.log10(rms(samples) / noise)
    assert snr > 30  # G.711 gives ~38 dB on a sine


def test_ulaw_round_trips() -> None:
    pcm = tone_pcm16le(8000, 0.25, 1000, beep_on=1, beep_off=0)
    samples = [int.from_bytes(pcm[i : i + 2], "little", signed=True) for i in range(0, len(pcm), 2)]
    decoded = [ulaw_to_linear(b) for b in pcm16le_to_ulaw(pcm)]
    noise = math.sqrt(sum((a - b) ** 2 for a, b in zip(samples, decoded, strict=True)) / len(samples))
    assert 20 * math.log10(rms(samples) / noise) > 30


def test_l16_big_endian() -> None:
    assert pcm16le_to_be(b"\x01\x02\x03\x04") == b"\x02\x01\x04\x03"
    assert Encoder("L16", 16000, l16_little_endian=True).encode(b"\x01\x02") == b"\x01\x02"


def test_tone_frequency() -> None:
    pcm = tone_pcm16le(8000, 1.0, 800)
    samples = [int.from_bytes(pcm[i : i + 2], "little", signed=True) for i in range(0, len(pcm), 2)]
    assert abs(dominant_frequency(samples) - 800) <= 10


# --------------------------------------------------------- against fake VTO


@pytest.fixture
async def vto():
    fake = FakeVto()
    await fake.start()
    yield fake
    await fake.stop()


async def test_describe_shows_talk_track(vto: FakeVto) -> None:
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    medias = await session.describe_only()
    back = find_backchannel(medias)
    assert back is not None and back.describe() == "L16/16000"


async def test_wrong_password(vto: FakeVto) -> None:
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "wrong")
    with pytest.raises(AuthError):
        await session.open()


async def test_tone_is_heard_with_pcma(vto: FakeVto) -> None:
    vto.set_codec("G.711A", 8000)
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    back = await session.open()
    assert back.describe() == "PCMA/8000"
    assert session.channel == 6
    talker = Talker(session, Encoder(back.encoding, back.clock_rate))
    started = asyncio.get_running_loop().time()
    await talker.play(tone_pcm16le(8000, 1.0, 800))
    elapsed = asyncio.get_running_loop().time() - started
    await session.close()
    await asyncio.sleep(0.05)

    talk = vto.talks[-1]
    assert talk.payload_type == 8
    # go2rtc-style packets: 1024 bytes, marker set, timestamp zero
    assert all(len(p) == 1024 and m == 1 and ts == 0 for _s, ts, m, p in talk.packets)
    seqs = [s for s, *_ in talk.packets]
    assert seqs == [(seqs[0] + i) & 0xFFFF for i in range(len(seqs))]
    heard = vto.heard_pcm()
    assert len(heard) >= 8000
    assert abs(dominant_frequency(heard[:8000]) - 800) <= 10
    # paced in real time, not dumped at once
    assert elapsed >= 0.9
    assert vto.talk_holder is None  # TEARDOWN released it


async def test_l16_track_is_silent_at_the_door(vto: FakeVto) -> None:
    """Reproduces the night's symptom: connected, nothing heard."""
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    back = await session.open()
    talker = Talker(session, Encoder(back.encoding, back.clock_rate))
    await talker.play(tone_pcm16le(back.clock_rate, 0.3, 800))
    await session.close()
    assert vto.talks[-1].packets  # bytes went out...
    assert vto.heard_pcm() == []  # ...but the speaker stayed silent


async def test_second_talker_is_refused(vto: FakeVto) -> None:
    vto.set_codec("G.711A", 8000)
    first = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    await first.open()
    second = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    with pytest.raises(BackchannelBusyError):
        await second.open()
    await first.close()
    await asyncio.sleep(0.05)
    third = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    await third.open()
    await third.close()


async def test_no_backchannel_raises() -> None:
    fake = FakeVto()
    fake._sdp = lambda backchannel, sub=False: FakeVto._sdp(fake, False, sub)  # type: ignore[method-assign]
    await fake.start()
    try:
        session = BackchannelSession("127.0.0.1", fake.rtsp_port, "admin", "T3st@pass")
        with pytest.raises(NoBackchannelError):
            await session.open()
    finally:
        await fake.stop()


async def test_live_feed_with_jitter(vto: FakeVto) -> None:
    """Mic chunks arriving unevenly still come out as clean, ordered audio."""
    vto.set_codec("G.711A", 8000)
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    back = await session.open()
    talker = Talker(session, Encoder(back.encoding, back.clock_rate), packet_bytes=320)
    runner = asyncio.create_task(talker.run())
    pcm = tone_pcm16le(8000, 1.5, 600, beep_on=1, beep_off=0)
    chunk = 320  # 20 ms
    for i in range(0, len(pcm), chunk):
        talker.feed_pcm(pcm[i : i + chunk])
        await asyncio.sleep(0.02 if (i // chunk) % 7 else 0.06)
    talker.finish()
    await asyncio.wait_for(runner, 5)
    await session.close()
    heard = vto.heard_pcm()
    assert len(heard) > 8000
    assert abs(dominant_frequency(heard[1000:9000]) - 600) <= 10


async def test_door_audio_arriving_during_talk_is_drained(vto: FakeVto) -> None:
    """With the door microphone track set up too, the VTO streams to us while we
    talk; that must neither block nor break the talk path."""
    vto.set_codec("G.711A", 8000)
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass", setup_receive_audio=True)
    back = await session.open()
    talker = Talker(session, Encoder(back.encoding, back.clock_rate), packet_bytes=320)
    await talker.play(tone_pcm16le(8000, 0.5, 900))
    assert session.bytes_received > 0  # the door's audio packet was read and dropped
    await session.close()
    assert abs(dominant_frequency(vto.heard_pcm()) - 900) <= 10
    assert any("trackID=1" in line for line in vto.log)


async def test_codec_revert_during_talk_does_not_break_session() -> None:
    fake = FakeVto(revert_after=0.3)
    await fake.start()
    try:
        fake.set_codec("G.711A", 8000)
        session = BackchannelSession("127.0.0.1", fake.rtsp_port, "admin", "T3st@pass")
        back = await session.open()
        talker = Talker(session, Encoder(back.encoding, back.clock_rate))
        await talker.play(tone_pcm16le(8000, 1.0, 700))
        await session.close()
        assert fake.compression == "PCM"  # it reverted meanwhile
        assert abs(dominant_frequency(fake.heard_pcm()[:8000]) - 700) <= 10
    finally:
        await fake.stop()


async def test_server_closing_mid_talk_is_reported(vto: FakeVto) -> None:
    vto.set_codec("G.711A", 8000)
    reasons: list = []
    session = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass", on_closed=reasons.append)
    back = await session.open()
    talker = Talker(session, Encoder(back.encoding, back.clock_rate))
    runner = asyncio.create_task(talker.run())
    talker.feed_pcm(tone_pcm16le(8000, 0.3, 500))
    await asyncio.sleep(0.2)
    await vto.stop()  # door station reboots / drops off the network
    vto.rtsp_server = None
    vto.http_runner = None
    await asyncio.wait_for(runner, 5)
    await asyncio.sleep(0.1)
    assert not session.is_open
    assert reasons and reasons[0] and "closed" in reasons[0]
    await session.close()


def test_gain_amplifies_and_limits_softly() -> None:
    from custom_components.dahua_vto_talk.vto.codecs import apply_gain_pcm16le, tone_pcm16le

    quiet = tone_pcm16le(8000, 0.5, 600, amplitude=0.05, beep_on=1, beep_off=0)
    loud = apply_gain_pcm16le(quiet, 12)
    def peak(b):
        return max(abs(int.from_bytes(b[i : i + 2], "little", signed=True)) for i in range(0, len(b), 2))
    assert 3.6 < peak(loud) / peak(quiet) < 4.3  # +12 dB is about x4
    # a full-scale input is bent by the limiter, never wraps around
    hot = apply_gain_pcm16le(tone_pcm16le(8000, 0.2, 600, amplitude=0.9, beep_on=1, beep_off=0), 24)
    assert 30000 < peak(hot) <= 32767
    assert apply_gain_pcm16le(quiet, 0) == quiet
