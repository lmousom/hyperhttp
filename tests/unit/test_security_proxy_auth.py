"""
``Proxy-Authorization`` is a hop-by-hop header (RFC 7235 §4.4). It must not
travel to the origin server under any circumstances — whether over a direct
connection or inside a CONNECT tunnel. The only legitimate carrier of the
header is the connection to the proxy itself, where the transport attaches
it from the proxy URL configuration.

These tests exercise the H1 ``_send`` path directly (where the stripping
happens) via a fake stream so we can inspect the bytes actually written to
the wire.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

from hyperhttp._headers import Headers
from hyperhttp._url import URL
from hyperhttp.connection.transport import H1Transport


class _CapturingStream:
    """Fake FastStream: captures writes, no-ops the rest."""

    def __init__(self) -> None:
        self.writes: List[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        return None

    async def readline(self) -> bytes:  # unused; response path not exercised
        return b""

    async def readexactly(self, n: int) -> bytes:
        return b""

    async def read(self, n: int) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True

    @property
    def all_bytes(self) -> bytes:
        return b"".join(self.writes)


def _make_h1(
    *, proxy_absolute_form: bool, proxy_auth_header: Optional[str] = None
) -> H1Transport:
    stream = _CapturingStream()
    # Bypass full init — we only exercise _send's header-construction path.
    t = H1Transport.__new__(H1Transport)
    t._stream = stream  # type: ignore[attr-defined]
    t._proxy_absolute_form = proxy_absolute_form
    t._proxy_auth_header = proxy_auth_header
    t._buffer_pool = None  # unused
    return t


def _send_sync(t: H1Transport, url: URL, headers: Headers) -> bytes:
    """Run the internal ``_send`` just far enough to capture the head bytes.

    A zero-length body means ``_send`` writes the head and exits; the
    capturing stream accumulates the wire bytes.
    """
    asyncio.run(t._send("GET", url, headers, None))
    return t._stream.all_bytes  # type: ignore[return-value]


def test_user_proxy_auth_stripped_on_origin_form() -> None:
    t = _make_h1(proxy_absolute_form=False)
    url = URL("https://api.example.com/secret")
    head = _send_sync(t, url, Headers([("Proxy-Authorization", "Basic leaked")]))
    assert b"Proxy-Authorization" not in head
    assert b"leaked" not in head


def test_user_proxy_auth_stripped_on_absolute_form_when_proxy_has_none() -> None:
    # Plain HTTP via HTTP proxy, no configured credential. User-supplied value
    # is discarded — the credential source must be the proxy URL, not the
    # incidental request headers.
    t = _make_h1(proxy_absolute_form=True, proxy_auth_header=None)
    url = URL("http://api.example.com/x")
    head = _send_sync(
        t, url, Headers([("Proxy-Authorization", "Basic attacker")])
    )
    assert b"Proxy-Authorization" not in head
    assert b"attacker" not in head


def _has_proxy_auth_value(head: bytes, expected_value: bytes) -> bool:
    # Header *name* casing is implementation-detail; match case-insensitively
    # on the name but preserve case on the value (which is the credential).
    for line in head.split(b"\r\n"):
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"proxy-authorization":
            return value.strip() == expected_value
    return False


def test_proxy_auth_attached_from_config_on_absolute_form() -> None:
    t = _make_h1(proxy_absolute_form=True, proxy_auth_header="Basic cnVubmVy")
    url = URL("http://api.example.com/x")
    head = _send_sync(t, url, Headers())
    assert _has_proxy_auth_value(head, b"Basic cnVubmVy")


def test_proxy_auth_config_wins_over_user_header_on_absolute_form() -> None:
    t = _make_h1(proxy_absolute_form=True, proxy_auth_header="Basic FROMCFG")
    url = URL("http://api.example.com/x")
    head = _send_sync(
        t,
        url,
        Headers([("Proxy-Authorization", "Basic FROMUSER")]),
    )
    assert _has_proxy_auth_value(head, b"Basic FROMCFG")
    assert b"FROMUSER" not in head


def test_caller_headers_not_mutated() -> None:
    """Stripping happens on a copy so the caller's request headers survive."""
    t = _make_h1(proxy_absolute_form=False)
    url = URL("https://api.example.com/x")
    caller_headers = Headers([("Proxy-Authorization", "Basic original")])
    _send_sync(t, url, caller_headers)
    assert caller_headers.get("proxy-authorization") == "Basic original"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
