"""Constants for Dahua VTO Talk."""

from __future__ import annotations

DOMAIN = "dahua_vto_talk"
VERSION = "0.2.1"

CONF_RTSP_PORT = "rtsp_port"
CONF_HTTP_PORT = "http_port"
CONF_CHANNEL = "channel"

# options
CONF_FORCE_CODEC = "force_codec_before_talk"
CONF_WATCHDOG_INTERVAL = "watchdog_interval"
CONF_PACKET_BYTES = "packet_bytes"
CONF_ZERO_TIMESTAMP = "zero_timestamp"
CONF_L16_LITTLE_ENDIAN = "l16_little_endian"
CONF_CANCEL_CALL_ENTITY = "cancel_call_entity"
CONF_SUBTYPE = "subtype"
CONF_SETUP_RECEIVE_AUDIO = "setup_receive_audio"
CONF_RESTORE_CODEC = "restore_codec_after_talk"
CONF_FIX_SUB_STREAM = "fix_sub_stream_audio"
DEFAULT_TALK_GAIN_DB = 0.0
CONF_RING_ENTITY = "ring_entity"
CONF_RING_TIMEOUT = "ring_timeout"
DEFAULT_RING_TIMEOUT = 60

CALL_IDLE = "idle"
CALL_RINGING = "ringing"
CALL_ANSWERED = "answered"
CALL_STATES = [CALL_IDLE, CALL_RINGING, CALL_ANSWERED]

DEFAULT_RTSP_PORT = 554
DEFAULT_HTTP_PORT = 80
DEFAULT_CHANNEL = 1
DEFAULT_WATCHDOG_INTERVAL = 30
DEFAULT_PACKET_BYTES = 1024

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"

TALK_IDLE = "idle"
TALK_CONNECTING = "connecting"
TALK_TALKING = "talking"
TALK_PLAYING = "playing"
TALK_ERROR = "error"
TALK_STATES = [TALK_IDLE, TALK_CONNECTING, TALK_TALKING, TALK_PLAYING, TALK_ERROR]

CARD_URL = f"/{DOMAIN}/dahua-vto-talk-card.js"
