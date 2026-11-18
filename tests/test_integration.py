"""Home Assistant level tests: config flow, entities, services and the websocket
talk path, all against the simulated VTO."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.dahua_vto_talk.const import (
    CONF_CANCEL_CALL_ENTITY,
    CONF_CHANNEL,
    CONF_HTTP_PORT,
    CONF_RTSP_PORT,
    DOMAIN,
)
from custom_components.dahua_vto_talk.vto.codecs import tone_pcm16le
from custom_components.dahua_vto_talk.vto.rtsp import BackchannelSession

from .fake_vto import SERIAL, FakeVto, dominant_frequency


@pytest.fixture
async def vto():
    fake = FakeVto()
    await fake.start()
    yield fake
    await fake.stop()


def entry_data(vto: FakeVto, password: str = "T3st@pass") -> dict:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_USERNAME: "admin",
        CONF_PASSWORD: password,
        CONF_RTSP_PORT: vto.rtsp_port,
        CONF_HTTP_PORT: vto.http_port,
        CONF_CHANNEL: 1,
    }


async def setup_entry(hass: HomeAssistant, vto: FakeVto, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(vto), options=options or {}, unique_id=SERIAL, title="VTO2202F")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def entity(hass: HomeAssistant, domain: str, key: str) -> str:
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id(domain, DOMAIN, f"{SERIAL}_{key}")
    assert entity_id, key
    return entity_id


# ------------------------------------------------------------- config flow


async def test_config_flow_creates_entry(hass: HomeAssistant, vto: FakeVto) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data(vto))
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "VTO2202F (127.0.0.1)"
    assert result["result"].unique_id == SERIAL


async def test_config_flow_wrong_password(hass: HomeAssistant, vto: FakeVto) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data(vto, "nope"))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_unreachable(hass: HomeAssistant, vto: FakeVto) -> None:
    data = entry_data(vto)
    data[CONF_RTSP_PORT] = 1  # nothing listens there
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow(hass: HomeAssistant, vto: FakeVto) -> None:
    entry = await setup_entry(hass, vto)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "force_codec_before_talk": True,
            "watchdog_interval": 60,
            "packet_bytes": "320",
            "zero_timestamp": False,
            "l16_little_endian": False,
            "setup_receive_audio": True,
            "subtype": "0",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["packet_bytes"] == 320
    assert entry.runtime_data.packet_bytes == 320
    assert entry.runtime_data.setup_receive_audio is True


# ------------------------------------------------------- entities/services


async def test_watchdog_sets_codec_and_counts_reverts(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto)
    assert vto.compression == "PCM"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (vto.compression, vto.frequency) == ("G.711A", 8000)
    assert hass.states.get(entity(hass, "sensor", "main_codec")).state == "G.711A/8000"
    assert hass.states.get(entity(hass, "sensor", "codec_reverts")).state == "0"
    # the sub stream gets sound in G.711A too, so a WebRTC preview is not silent
    assert (vto.sub_enabled, vto.sub_compression, vto.sub_frequency) == ("true", "G.711A", "8000")
    # and the sub stream's own RTSP description now really offers that sound
    assert hass.states.get(entity(hass, "sensor", "sub_stream_audio")).state == "PCMA/8000"

    # the real VTO went back to PCM on its own; the watchdog notices and fixes it
    vto.compression, vto.frequency = "PCM", 16000
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert vto.compression == "G.711A"
    assert hass.states.get(entity(hass, "sensor", "codec_reverts")).state == "1"


async def test_keep_codec_switch_off_stops_watchdog(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto)
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity(hass, "switch", "keep_codec")}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert vto.compression == "PCM"


async def test_test_tone_button_is_heard(hass: HomeAssistant, vto: FakeVto) -> None:
    """Device starts in PCM (like the real one); the button must still be heard."""
    await setup_entry(hass, vto)
    assert vto.compression == "PCM"
    await hass.services.async_call("button", "press", {"entity_id": entity(hass, "button", "test_tone")}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert vto.compression == "G.711A"
    talk = vto.talks[-1]
    assert talk.payload_type == 8
    heard = vto.heard_pcm(talk)
    assert len(heard) >= 3 * 8000 * 0.95
    assert abs(dominant_frequency(heard[:8000]) - 800) <= 10
    assert hass.states.get(entity(hass, "sensor", "talk_status")).state == "idle"
    assert hass.states.get(entity(hass, "sensor", "backchannel_format")).state == "PCMA/8000"
    assert vto.talk_holder is None


async def test_small_packet_tone(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto)
    await hass.services.async_call(
        DOMAIN, "play_test_tone", {"seconds": 1, "frequency": 1000, "packet_bytes": 160, "zero_timestamp": False}, blocking=True
    )
    talk = vto.talks[-1]
    assert all(len(p) == 160 for *_x, p in talk.packets)
    stamps = [ts for _s, ts, _m, _p in talk.packets]
    assert stamps[1] - stamps[0] == 160
    assert abs(dominant_frequency(vto.heard_pcm(talk)[:4000]) - 1000) <= 10


async def test_http_tone(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto)
    await hass.services.async_call(DOMAIN, "play_test_tone", {"seconds": 0.5, "transport": "http"}, blocking=True)
    content_type, data = vto.post_audio[-1]
    assert content_type == "Audio/G.711A"
    assert len(data) == 4000


async def test_fix_codec_service_response(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto)
    result = await hass.services.async_call(DOMAIN, "fix_codec", {}, blocking=True, return_response=True)
    assert result == {"changed": True, "main_codec": "G.711A/8000", "backchannel": "PCMA/8000"}


async def test_cancel_call_entity_is_pressed(hass: HomeAssistant, vto: FakeVto) -> None:
    assert await async_setup_component(hass, "input_button", {"input_button": {"hangup": {}}})
    before = hass.states.get("input_button.hangup").state
    await setup_entry(hass, vto, {CONF_CANCEL_CALL_ENTITY: "input_button.hangup"})
    await hass.services.async_call(DOMAIN, "play_test_tone", {"seconds": 0.5}, blocking=True)
    assert hass.states.get("input_button.hangup").state != before


async def test_busy_backchannel_reports_clear_error(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto)
    vto.set_codec("G.711A", 8000)
    other = BackchannelSession("127.0.0.1", vto.rtsp_port, "admin", "T3st@pass")
    await other.open()
    try:
        from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

        with pytest.raises(HomeAssistantError, match="zajęty"):
            await hass.services.async_call(DOMAIN, "play_test_tone", {"seconds": 0.5}, blocking=True)
        assert hass.states.get(entity(hass, "sensor", "talk_status")).state == "error"
    finally:
        await other.close()


# ----------------------------------------------------------- websocket talk


async def test_sub_stream_fix_can_be_turned_off(hass: HomeAssistant, vto: FakeVto) -> None:
    await setup_entry(hass, vto, {"fix_sub_stream_audio": False})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert vto.compression == "G.711A"
    assert (vto.sub_enabled, vto.sub_compression) == ("false", "PCM")
    assert hass.states.get(entity(hass, "sensor", "sub_stream_audio")).state == "unknown"


async def test_sub_stream_audio_probe_reports_missing_sound(hass: HomeAssistant, vto: FakeVto) -> None:
    entry = await setup_entry(hass, vto, {"fix_sub_stream_audio": False})
    assert await entry.runtime_data.probe_sub_stream_audio() == "none"
    vto.sub_enabled, vto.sub_compression, vto.sub_frequency = "true", "PCM", "16000"
    assert await entry.runtime_data.probe_sub_stream_audio() == "L16/16000"


async def test_websocket_talk_end_to_end(hass: HomeAssistant, vto: FakeVto, hass_ws_client, hass_client) -> None:
    """Browser mic -> websocket binary frames -> HA -> RTSP backchannel -> door speaker."""
    await setup_entry(hass, vto)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": f"{DOMAIN}/list"})
    listed = await client.receive_json()
    assert listed["success"] and listed["result"][0]["host"] == "127.0.0.1"

    await client.send_json({"id": 2, "type": f"{DOMAIN}/talk"})
    result = await client.receive_json()
    assert result["success"], result
    ready = await client.receive_json()
    assert ready["event"]["event"] == "ready", ready
    assert ready["event"]["sample_rate"] == 8000
    assert ready["event"]["codec"] == "PCMA/8000"
    handler_id = ready["event"]["handler_id"]
    assert hass.states.get(entity(hass, "sensor", "talk_status")).state == "talking"

    # what the card sends: 20 ms of 16-bit LE PCM per frame, in real time
    pcm = tone_pcm16le(8000, 2.0, 600, beep_on=1, beep_off=0)
    frame = 320
    for i in range(0, len(pcm), frame):
        await client.send_bytes(bytes([handler_id]) + pcm[i : i + frame])
        await asyncio.sleep(0.02)

    stats = None
    for _ in range(5):
        msg = await asyncio.wait_for(client.receive_json(), 3)
        if msg.get("event", {}).get("event") == "stats":
            stats = msg["event"]
            break
    assert stats and stats["packets_sent"] > 0

    await client.send_json({"id": 3, "type": "unsubscribe_events", "subscription": 2})
    for _ in range(10):
        msg = await asyncio.wait_for(client.receive_json(), 3)
        if msg.get("id") == 3:
            break
    for _ in range(50):
        await asyncio.sleep(0.05)
        if vto.talk_holder is None:
            break
    await hass.async_block_till_done(wait_background_tasks=True)

    heard = vto.heard_pcm()
    assert len(heard) >= 8000 * 1.5
    assert abs(dominant_frequency(heard[2000:10000]) - 600) <= 10
    assert vto.talk_holder is None, "talk track must be released when the card stops"
    assert hass.states.get(entity(hass, "sensor", "talk_status")).state == "idle"

    # the card's "play back what went to the gate": exactly what was sent
    http = await hass_client()
    resp = await http.get("/api/dahua_vto_talk/last_talk.wav")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "audio/wav"
    body = await resp.read()
    import io, wave  # noqa: E401, PLC0415

    with wave.open(io.BytesIO(body)) as wav:
        assert wav.getframerate() == 8000
        frames = wav.readframes(wav.getnframes())
    assert frames == pcm
    samples = [int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)]
    assert abs(dominant_frequency(samples[2000:10000]) - 600) <= 10


async def test_websocket_talk_error_event(hass: HomeAssistant, vto: FakeVto, hass_ws_client) -> None:
    await setup_entry(hass, vto)
    vto.password = "changed-on-device"
    client = await hass_ws_client(hass)
    await client.send_json({"id": 5, "type": f"{DOMAIN}/talk"})
    assert (await client.receive_json())["success"]
    event = (await asyncio.wait_for(client.receive_json(), 5))["event"]
    assert event["event"] == "error"
    assert "hasło" in event["message"]


async def test_disconnect_releases_talk_track(hass: HomeAssistant, vto: FakeVto, hass_ws_client) -> None:
    await setup_entry(hass, vto)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 7, "type": f"{DOMAIN}/talk"})
    await client.receive_json()
    ready = await client.receive_json()
    assert ready["event"]["event"] == "ready"
    assert vto.talk_holder is not None
    await client.close()
    for _ in range(60):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done(wait_background_tasks=True)
        if vto.talk_holder is None:
            break
    assert vto.talk_holder is None


async def test_unload(hass: HomeAssistant, vto: FakeVto) -> None:
    entry = await setup_entry(hass, vto)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_restore_codec_after_talk(hass: HomeAssistant, vto: FakeVto) -> None:
    from custom_components.dahua_vto_talk.const import CONF_RESTORE_CODEC  # noqa: PLC0415

    await setup_entry(hass, vto, {CONF_RESTORE_CODEC: True})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert vto.compression == "PCM", "watchdog must stay away in restore mode"
    await hass.services.async_call(DOMAIN, "play_test_tone", {"seconds": 0.5}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert vto.talks[-1].payload_type == 8  # talked in G.711A...
    assert abs(dominant_frequency(vto.heard_pcm()) - 800) <= 10
    assert (vto.compression, vto.frequency) == ("PCM", 16000)  # ...and gave DMSS its PCM back


async def test_diagnostics(hass: HomeAssistant, vto: FakeVto) -> None:
    from custom_components.dahua_vto_talk.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    entry = await setup_entry(hass, vto)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["password"] == "**REDACTED**"
    assert diag["rtsp_describe"]["backchannel"] in ("L16/16000", "PCMA/8000")
    assert diag["rtsp_describe"]["medias"][3]["control"] == "trackID=5"
    assert "table.Encode[0].MainFormat[0].Audio.Compression" in diag["encode_audio"]
