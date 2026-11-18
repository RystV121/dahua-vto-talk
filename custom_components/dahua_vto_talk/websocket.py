"""Websocket API used by the Lovelace card to stream the microphone.

The card subscribes to ``dahua_vto_talk/talk``. Once the door's talk track is
open it gets ``{"event": "ready", "handler_id", "sample_rate", ...}`` and from
then on sends binary websocket frames: one byte ``handler_id`` followed by
16-bit little-endian mono PCM at ``sample_rate``. This is the same binary
channel Home Assistant's own Assist pipeline uses for microphone audio.
Unsubscribing (or the browser going away) closes the talk track.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_talk)
    websocket_api.async_register_command(hass, ws_status)
    websocket_api.async_register_command(hass, ws_client_report)
    websocket_api.async_register_command(hass, ws_subscribe_call)
    websocket_api.async_register_command(hass, ws_call_action)


def _device(hass: HomeAssistant, entry_id: str | None):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    return entries[0].runtime_data if entries else None


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list"})
@callback
def ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(
        msg["id"],
        [
            {"entry_id": e.entry_id, "title": e.title, "host": e.runtime_data.host}
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
        ],
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/status", vol.Optional("entry_id"): str})
@callback
def ws_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    device = _device(hass, msg.get("entry_id"))
    if device is None:
        connection.send_error(msg["id"], "not_found", "Dahua VTO Talk is not set up")
        return
    connection.send_result(msg["id"], device.diagnostics())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/talk",
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_talk(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    device = _device(hass, msg.get("entry_id"))
    if device is None:
        connection.send_error(msg["id"], "not_found", "Dahua VTO Talk is not set up")
        return

    msg_id = msg["id"]
    state: dict[str, Any] = {"handle": None, "unregister": None, "closed": False}

    def send(event: dict[str, Any]) -> None:
        if not state["closed"]:
            connection.send_message(websocket_api.event_message(msg_id, event))

    @callback
    def on_binary(_hass: HomeAssistant, _conn: websocket_api.ActiveConnection, payload: bytes) -> None:
        handle = state["handle"]
        if handle is not None:
            handle.talker.feed_pcm(payload)

    async def run() -> None:
        try:
            handle = await device.open_talk("card")
        except HomeAssistantError as err:
            send({"event": "error", "message": str(err)})
            return
        state["handle"] = handle
        handler_id, unregister = connection.async_register_binary_handler(on_binary)
        state["unregister"] = unregister
        send(
            {
                "event": "ready",
                "handler_id": handler_id,
                "sample_rate": handle.sample_rate,
                "codec": repr(handle.encoder),
                "packet_ms": round(handle.talker.packet_seconds * 1000),
            }
        )
        try:
            while not handle.task.done():
                await asyncio.sleep(1)
                send({"event": "stats", **handle.stats()})
            reason = handle.session.close_reason
            send({"event": "ended", "reason": reason or "zakończono"})
        except asyncio.CancelledError:
            raise

    task = hass.async_create_background_task(run(), f"{DOMAIN} talk {msg_id}")

    @callback
    def unsubscribe() -> None:
        state["closed"] = True
        task.cancel()
        if state["unregister"] is not None:
            state["unregister"]()

        async def cleanup() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            handle = state["handle"]
            if handle is not None:
                await device.stop_talk(handle)
            elif device.active is not None and device.active.client == "card":
                # cancelled right as the channel opened
                await device.stop_talk(device.active)

        hass.async_create_background_task(cleanup(), f"{DOMAIN} talk cleanup {msg_id}")

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/client_report",
        vol.Optional("entry_id"): str,
        vol.Required("kind"): vol.In(["talk", "preview"]),
        vol.Required("data"): dict,
    }
)
@callback
def ws_client_report(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What the card saw in the browser (mic, audio context, preview player)."""
    device = _device(hass, msg.get("entry_id"))
    if device is None:
        connection.send_error(msg["id"], "not_found", "Dahua VTO Talk is not set up")
        return
    device.store_client_report(msg["kind"], msg["data"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe_call"})
@callback
def ws_subscribe_call(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Call state of every door station: drives the "someone is ringing" popup."""
    msg_id = msg["id"]
    removers = []

    @callback
    def forward(info: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, info))

    connection.send_result(msg_id)
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        removers.append(entry.runtime_data.add_call_listener(forward))

    @callback
    def unsubscribe() -> None:
        for remove in removers:
            remove()

    connection.subscriptions[msg_id] = unsubscribe


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/call_action",
        vol.Optional("entry_id"): str,
        vol.Required("action"): vol.In(["answer", "hangup", "decline", "ring"]),
        vol.Optional("client"): str,
    }
)
@callback
def ws_call_action(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    device = _device(hass, msg.get("entry_id"))
    if device is None:
        connection.send_error(msg["id"], "not_found", "Dahua VTO Talk is not set up")
        return
    if msg["action"] == "ring":
        device.start_ring("card")
    else:
        device.call_action(msg["action"], msg.get("client"))
    connection.send_result(msg["id"], device.call_info())
