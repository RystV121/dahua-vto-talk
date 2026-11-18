"""HTTP/RTSP Digest authentication (RFC 2617), as Dahua devices speak it.

Dahua uses two flavours:

* RTSP: ``Digest realm="Login to <id>", nonce="..."`` -- no qop. The nonce is
  only valid on the TCP connection that received it (measured on a
  VTO2202F: answering the challenge on a new connection gives 401 again).
* HTTP CGI: ``Digest realm="...", qop="auth", nonce="...", opaque="..."``.

Both are handled here.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

_PARAM_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))')


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 - protocol


def parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    """Return (scheme, params) from a WWW-Authenticate header value."""
    header = header.strip()
    scheme, _, rest = header.partition(" ")
    params = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _PARAM_RE.finditer(rest)}
    return scheme.lower(), params


def pick_challenge(values: list[str]) -> str | None:
    """Prefer a Digest challenge when a server sends several headers."""
    for value in values:
        if value.lower().startswith("digest"):
            return value
    return values[0] if values else None


@dataclass
class DigestAuth:
    """Builds Authorization headers for one challenge, counting nonce use."""

    username: str
    password: str
    scheme: str = "digest"
    params: dict[str, str] = field(default_factory=dict)
    _nc: int = 0

    @classmethod
    def from_header(cls, username: str, password: str, header: str) -> DigestAuth:
        scheme, params = parse_challenge(header)
        return cls(username, password, scheme, params)

    def authorization(self, method: str, uri: str) -> str:
        """The value of the Authorization header for this request."""
        if self.scheme == "basic":
            import base64  # noqa: PLC0415

            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            return f"Basic {token}"

        realm = self.params.get("realm", "")
        nonce = self.params.get("nonce", "")
        algorithm = self.params.get("algorithm", "MD5")
        ha1 = _md5(f"{self.username}:{realm}:{self.password}")
        if algorithm.upper() == "MD5-SESS":
            cnonce_sess = os.urandom(8).hex()
            ha1 = _md5(f"{ha1}:{nonce}:{cnonce_sess}")
        ha2 = _md5(f"{method}:{uri}")

        qop_raw = self.params.get("qop")
        parts = [
            f'username="{self.username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
        ]
        if qop_raw:
            qops = [q.strip() for q in qop_raw.split(",")]
            qop = "auth" if "auth" in qops else qops[0]
            self._nc += 1
            nc = f"{self._nc:08x}"
            cnonce = os.urandom(8).hex()
            response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            parts += [f'response="{response}"', f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            response = _md5(f"{ha1}:{nonce}:{ha2}")
            parts.append(f'response="{response}"')
        if "opaque" in self.params:
            parts.append(f'opaque="{self.params["opaque"]}"')
        if "algorithm" in self.params:
            parts.append(f"algorithm={algorithm}")
        return "Digest " + ", ".join(parts)
