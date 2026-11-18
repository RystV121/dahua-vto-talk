"""Audio encoders and helpers, pure Python (no audioop: it is gone in 3.13).

Input everywhere is signed 16-bit little-endian mono PCM (what the browser
card sends). Output is whatever the door station's backchannel asked for.
"""

from __future__ import annotations

import array
import math
import sys

ALAW_SILENCE = 0xD5
ULAW_SILENCE = 0xFF

_BIAS = 0x84
_CLIP = 32635


def _linear_to_alaw(pcm: int) -> int:
    """G.711 A-law, the same algorithm go2rtc uses (pkg/pcm/pcma.go)."""
    if pcm < 0:
        pcm = -pcm
        sign = 0x80
    else:
        sign = 0
    if pcm > 0x7FFF:
        pcm = 0x7FFF
    exponent = 7
    mask = 0x4000
    while (pcm & mask) == 0 and exponent > 0:
        exponent -= 1
        mask >>= 1
    if exponent == 0:
        value = sign | ((pcm >> 4) & 0x0F)
    else:
        value = sign | (exponent << 4) | ((pcm >> (exponent + 3)) & 0x0F)
    return value ^ 0xD5


def _linear_to_ulaw(pcm: int) -> int:
    """G.711 mu-law (Sun reference implementation)."""
    sign = 0x80 if pcm < 0 else 0
    if pcm < 0:
        pcm = -pcm
    if pcm > _CLIP:
        pcm = _CLIP
    pcm += _BIAS
    exponent = 7
    mask = 0x4000
    while (pcm & mask) == 0 and exponent > 0:
        exponent -= 1
        mask >>= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def alaw_to_linear(value: int) -> int:
    """Decode one A-law byte (used by tests and the simulator)."""
    value ^= 0xD5
    data = ((value & 0x0F) << 4) + 8
    exponent = (value & 0x70) >> 4
    if exponent:
        data |= 0x100
    if exponent > 1:
        data <<= exponent - 1
    return -data if value & 0x80 else data


def ulaw_to_linear(value: int) -> int:
    value = ~value & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


# Lookup tables indexed by the unsigned 16-bit view of a sample.
_ALAW = bytes(_linear_to_alaw(i - 65536 if i > 32767 else i) for i in range(65536))
_ULAW = bytes(_linear_to_ulaw(i - 65536 if i > 32767 else i) for i in range(65536))


def _samples(pcm_le: bytes) -> array.array:
    if len(pcm_le) % 2:
        pcm_le = pcm_le[:-1]
    samples = array.array("H")
    samples.frombytes(pcm_le)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def pcm16le_to_alaw(pcm_le: bytes) -> bytes:
    table = _ALAW
    return bytes(table[s] for s in _samples(pcm_le))


def pcm16le_to_ulaw(pcm_le: bytes) -> bytes:
    table = _ULAW
    return bytes(table[s] for s in _samples(pcm_le))


def pcm16le_to_be(pcm_le: bytes) -> bytes:
    """RFC 3551 L16 is big-endian."""
    if len(pcm_le) % 2:
        pcm_le = pcm_le[:-1]
    swapped = bytearray(len(pcm_le))
    swapped[0::2] = pcm_le[1::2]
    swapped[1::2] = pcm_le[0::2]
    return bytes(swapped)


def resample_pcm16le(pcm_le: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampler for short clips (tones, files)."""
    if src_rate == dst_rate or not pcm_le:
        return pcm_le
    src = array.array("h")
    src.frombytes(pcm_le[: len(pcm_le) - len(pcm_le) % 2])
    if sys.byteorder == "big":
        src.byteswap()
    out_len = int(len(src) * dst_rate / src_rate)
    out = array.array("h", bytes(out_len * 2))
    step = src_rate / dst_rate
    last = len(src) - 1
    for i in range(out_len):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j < last else a
        out[i] = int(a + (b - a) * frac)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def tone_pcm16le(
    sample_rate: int,
    seconds: float,
    frequency: float = 800.0,
    amplitude: float = 0.5,
    beep_on: float = 0.25,
    beep_off: float = 0.15,
) -> bytes:
    """A beeping sine: easy to recognise at the door, and to check by ear.

    A correct format sounds like clean beeps at the right pitch. The wrong
    byte order sounds like loud noise; the wrong rate sounds like a lower or
    higher pitch.
    """
    total = int(sample_rate * seconds)
    period = beep_on + beep_off
    out = array.array("h", bytes(total * 2))
    peak = int(32767 * max(0.0, min(1.0, amplitude)))
    fade = max(1, int(sample_rate * 0.005))
    on_len = int(sample_rate * beep_on)
    for i in range(total):
        t = i / sample_rate
        in_period = t % period
        if period and in_period >= beep_on:
            continue
        k = int(in_period * sample_rate)
        env = min(1.0, k / fade, max(0, on_len - k) / fade)
        out[i] = int(peak * env * math.sin(2 * math.pi * frequency * t))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


class Encoder:
    """Encodes PCM16LE for one negotiated backchannel format."""

    def __init__(self, encoding: str, clock_rate: int, l16_little_endian: bool = False) -> None:
        self.encoding = encoding.upper()
        self.clock_rate = clock_rate
        self.l16_little_endian = l16_little_endian
        if self.encoding == "PCMA":
            self.bytes_per_sample = 1
            self.silence = bytes([ALAW_SILENCE])
        elif self.encoding == "PCMU":
            self.bytes_per_sample = 1
            self.silence = bytes([ULAW_SILENCE])
        elif self.encoding in ("L16", "PCM", "PCML"):
            self.bytes_per_sample = 2
            self.silence = b"\x00\x00"
        else:
            raise ValueError(f"unsupported backchannel codec {encoding}/{clock_rate}")

    def encode(self, pcm_le: bytes) -> bytes:
        if self.encoding == "PCMA":
            return pcm16le_to_alaw(pcm_le)
        if self.encoding == "PCMU":
            return pcm16le_to_ulaw(pcm_le)
        if self.encoding == "PCML" or self.l16_little_endian:
            return pcm_le[: len(pcm_le) - len(pcm_le) % 2]
        return pcm16le_to_be(pcm_le)

    @property
    def byte_rate(self) -> int:
        return self.clock_rate * self.bytes_per_sample

    def __repr__(self) -> str:
        suffix = " LE" if self.encoding == "L16" and self.l16_little_endian else ""
        return f"{self.encoding}/{self.clock_rate}{suffix}"


def apply_gain_pcm16le(pcm_le: bytes, gain_db: float) -> bytes:
    """Amplify 16-bit LE PCM by ``gain_db`` with a soft limiter.

    Speech from a phone/tablet mic (after its automatic gain control) sits
    15-20 dB below the test tone, which can be too quiet on a small door
    speaker outdoors. Above 70 % of full scale the curve bends smoothly
    (tanh) instead of clipping, so loud syllables do not crackle.
    """
    if not gain_db or len(pcm_le) < 2:
        return pcm_le
    import math  # noqa: PLC0415
    from array import array  # noqa: PLC0415

    g = 10 ** (gain_db / 20)
    samples = array("h")
    samples.frombytes(pcm_le[: len(pcm_le) - len(pcm_le) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    knee = 0.7 * 32767
    room = 32767 - knee
    out = array("h", bytes(len(samples) * 2))
    for i, s in enumerate(samples):
        y = s * g
        a = abs(y)
        if a > knee:
            a = knee + room * math.tanh((a - knee) / room)
            y = a if y > 0 else -a
        out[i] = int(y)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()
