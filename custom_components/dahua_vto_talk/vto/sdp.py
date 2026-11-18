"""Just enough SDP parsing to find a Dahua VTO's ONVIF audio backchannel.

ONVIF Streaming Spec 5.3: direction attributes are from the *client's* point
of view. The streams the camera sends are ``a=recvonly``; the backchannel the
client sends on is ``a=sendonly``. A real VTO2202F (firmware 4.511, 2024-07-31)
answered a DESCRIBE carrying ``Require: www.onvif.org/ver20/backchannel`` with::

    m=audio 0 RTP/AVP 97          <- door microphone, a=recvonly
    a=control:trackID=1
    a=rtpmap:97 L16/16000
    ...
    m=audio 0 RTP/AVP 97          <- backchannel, a=sendonly
    a=control:trackID=5
    a=rtpmap:97 L16/16000

and with ``8 PCMA/8000`` on both once the main stream's audio was switched
to G.711A at 8 kHz.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATIC_PT = {0: ("PCMU", 8000), 8: ("PCMA", 8000), 10: ("L16", 44100), 11: ("L16", 44100)}


@dataclass
class SdpMedia:
    """One ``m=`` section."""

    index: int
    kind: str
    payload_types: list[int]
    control: str = ""
    direction: str = "sendrecv"
    rtpmap: dict[int, str] = field(default_factory=dict)

    @property
    def payload_type(self) -> int:
        return self.payload_types[0] if self.payload_types else -1

    @property
    def encoding(self) -> str:
        """Codec name in upper case, e.g. ``PCMA`` or ``L16``."""
        return self._format()[0]

    @property
    def clock_rate(self) -> int:
        return self._format()[1]

    @property
    def channels(self) -> int:
        return self._format()[2]

    def _format(self) -> tuple[str, int, int]:
        pt = self.payload_type
        value = self.rtpmap.get(pt)
        if value:
            parts = value.split("/")
            name = parts[0].upper()
            try:
                rate = int(parts[1]) if len(parts) > 1 else 8000
            except ValueError:
                rate = 8000
            try:
                ch = int(parts[2]) if len(parts) > 2 else 1
            except ValueError:
                ch = 1
            return name, rate, ch
        if pt in STATIC_PT:
            name, rate = STATIC_PT[pt]
            return name, rate, 1
        return "", 0, 1

    def describe(self) -> str:
        return f"{self.encoding}/{self.clock_rate}" if self.encoding else f"PT{self.payload_type}"


def parse_sdp(text: str) -> list[SdpMedia]:
    """All media sections of an SDP body."""
    medias: list[SdpMedia] = []
    current: SdpMedia | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("m="):
            parts = line[2:].split()
            pts = [int(p) for p in parts[3:] if p.isdigit()]
            current = SdpMedia(index=len(medias), kind=parts[0].lower() if parts else "", payload_types=pts)
            medias.append(current)
        elif current is None:
            continue
        elif line.startswith("a=control:"):
            current.control = line[len("a=control:") :].strip()
        elif line.startswith("a=rtpmap:"):
            pt, _, value = line[len("a=rtpmap:") :].partition(" ")
            if pt.isdigit():
                current.rtpmap[int(pt)] = value.strip()
        elif line in ("a=sendonly", "a=recvonly", "a=sendrecv", "a=inactive"):
            current.direction = line[2:]
    return medias


def find_backchannel(medias: list[SdpMedia]) -> SdpMedia | None:
    """The audio section the client is meant to send on, if any."""
    for media in medias:
        if media.kind == "audio" and media.direction == "sendonly":
            return media
    return None


def find_receive_audio(medias: list[SdpMedia]) -> SdpMedia | None:
    for media in medias:
        if media.kind == "audio" and media.direction in ("recvonly", "sendrecv"):
            return media
    return None
