"""Turns a stream (or clip) of PCM into paced RTP on an open backchannel."""

from __future__ import annotations

import asyncio
import logging
import time

from .codecs import Encoder
from .rtsp import BackchannelSession

_LOGGER = logging.getLogger(__name__)

# go2rtc repacks every G.711 backchannel into 1024-byte payloads before it
# sends them to a Dahua/Reolink doorbell (pkg/pcm/handlers.go RepackG711).
DEFAULT_PACKET_BYTES = 1024


class Talker:
    """Buffers encoded audio and sends it at the speed the speaker plays it."""

    def __init__(
        self,
        session: BackchannelSession,
        encoder: Encoder,
        packet_bytes: int = DEFAULT_PACKET_BYTES,
        max_buffer_ms: int = 800,
        lead_ms: int = 60,
    ) -> None:
        self.session = session
        self.encoder = encoder
        bps = encoder.bytes_per_sample
        self.packet_bytes = max(bps, packet_bytes - packet_bytes % bps)
        self.samples_per_packet = self.packet_bytes // bps
        self.packet_seconds = self.samples_per_packet / encoder.clock_rate
        self.max_buffer = int(encoder.byte_rate * max_buffer_ms / 1000)
        self.lead = lead_ms / 1000
        self._buffer = bytearray()
        self._wake = asyncio.Event()
        self._finishing = False
        self.dropped_bytes = 0
        self.received_bytes = 0
        self.peak = 0

    @property
    def buffered_ms(self) -> int:
        return int(len(self._buffer) / self.encoder.byte_rate * 1000)

    def feed_pcm(self, pcm_le: bytes) -> None:
        """Add 16-bit LE mono PCM at the encoder's clock rate."""
        if not pcm_le or self._finishing:
            return
        self.received_bytes += len(pcm_le)
        self._update_peak(pcm_le)
        self._buffer += self.encoder.encode(pcm_le)
        overflow = len(self._buffer) - self.max_buffer
        if overflow > 0:
            # Fell behind (network burst, slow tab): drop the oldest audio so
            # the delay does not keep growing.
            cut = overflow + (-overflow % self.encoder.bytes_per_sample)
            del self._buffer[:cut]
            self.dropped_bytes += cut
        self._wake.set()

    def _update_peak(self, pcm_le: bytes) -> None:
        step = max(2, (len(pcm_le) // 64) & ~1)
        peak = 0
        for i in range(0, len(pcm_le) - 1, step):
            v = abs(int.from_bytes(pcm_le[i : i + 2], "little", signed=True))
            peak = max(peak, v)
        self.peak = peak

    def finish(self) -> None:
        """Send what is buffered (padded with silence) and stop."""
        self._finishing = True
        self._wake.set()

    async def run(self) -> None:
        """Send packets until finish() drains the buffer or the session dies."""
        next_send: float | None = None
        while True:
            if not self.session.is_open:
                return
            if len(self._buffer) < self.packet_bytes:
                if self._finishing:
                    if self._buffer:
                        pad = self.packet_bytes - len(self._buffer)
                        self._buffer += self.encoder.silence * (pad // self.encoder.bytes_per_sample)
                    else:
                        return
                else:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), 1.0)
                    except TimeoutError:
                        pass
                    continue
            now = time.monotonic()
            if next_send is None or now - next_send > self.packet_seconds:
                # first packet, or we starved: restart the clock instead of
                # bursting to "catch up"
                next_send = now
            delay = next_send - self.lead - now
            if delay > 0:
                await asyncio.sleep(delay)
            payload = bytes(self._buffer[: self.packet_bytes])
            del self._buffer[: self.packet_bytes]
            self.session.send_payload(payload, self.samples_per_packet)
            await self.session.drain()
            next_send += self.packet_seconds

    async def play(self, pcm_le: bytes) -> None:
        """Send a whole clip at real-time speed."""
        self._buffer += self.encoder.encode(pcm_le)
        self.finish()
        await self.run()
        # let the last packet reach the speaker before a TEARDOWN cuts it
        await asyncio.sleep(min(1.0, self.packet_seconds * 2))
