"""
Redirect-time credential-leak protections.

On a redirect, sensitive headers (Authorization, Proxy-Authorization, Cookie)
must not be forwarded to a different origin, and must not be forwarded across
an HTTPS → HTTP scheme downgrade even within the same host.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import hyperhttp
from hyperhttp._url import URL
from hyperhttp.client import _is_credential_leak_redirect


# ---------------------------------------------------------------------------
# Pure helper: origin/downgrade detection
# ---------------------------------------------------------------------------


def test_same_origin_is_safe() -> None:
    assert not _is_credential_leak_redirect(
        URL("https://api.example.com/a"), URL("https://api.example.com/b")
    )


def test_cross_host_is_unsafe() -> None:
    assert _is_credential_leak_redirect(
        URL("https://api.example.com/"), URL("https://evil.example.com/")
    )


def test_cross_subdomain_is_unsafe() -> None:
    # Subdomain mismatch is still cross-origin for credential purposes.
    assert _is_credential_leak_redirect(
        URL("https://a.example.com/"), URL("https://b.example.com/")
    )


def test_cross_port_is_unsafe() -> None:
    assert _is_credential_leak_redirect(
        URL("https://x.example.com/"), URL("https://x.example.com:8443/")
    )


def test_scheme_downgrade_is_unsafe_same_host() -> None:
    assert _is_credential_leak_redirect(
        URL("https://x.example.com/"), URL("http://x.example.com/")
    )


def test_scheme_upgrade_is_safe_same_host() -> None:
    # http → https within the same host is still cross-origin because the
    # default port differs (80 → 443); credentials are stripped either way.
    assert _is_credential_leak_redirect(
        URL("http://x.example.com/"), URL("https://x.example.com/")
    )


def test_host_case_is_ignored() -> None:
    assert not _is_credential_leak_redirect(
        URL("https://Example.COM/"), URL("https://example.com/")
    )


# ---------------------------------------------------------------------------
# End-to-end: via MockTransport with a scripted redirect chain
# ---------------------------------------------------------------------------


class _Recorder:
    """Handler that scripts a redirect chain and records every request."""

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.script = script
        self.requests: List[hyperhttp.client.Request] = []
        self._i = 0

    def __call__(self, request: hyperhttp.client.Request) -> hyperhttp.MockResponse:
        self.requests.append(request)
        step = self.script[self._i]
        self._i += 1
        return hyperhttp.MockResponse(**step)


async def test_authorization_stripped_on_cross_origin_redirect() -> None:
    recorder = _Recorder(
        [
            {
                "status_code": 302,
                "headers": {"Location": "https://evil.example.com/pwn"},
            },
            {"status_code": 200, "text": "ok"},
        ]
    )
    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(recorder),
        follow_redirects=True,
    ) as c:
        r = await c.get(
            "https://api.example.com/secret",
            headers={"Authorization": "Bearer s3cret"},
        )
        assert r.status_code == 200

    assert len(recorder.requests) == 2
    first, second = recorder.requests
    assert first.headers.get("authorization") == "Bearer s3cret"
    assert second.headers.get("authorization") is None


async def test_cookie_header_stripped_on_cross_origin_redirect() -> None:
    recorder = _Recorder(
        [
            {"status_code": 302, "headers": {"Location": "https://evil/x"}},
            {"status_code": 200, "text": "ok"},
        ]
    )
    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(recorder),
        follow_redirects=True,
    ) as c:
        await c.get(
            "https://api.example.com/", headers={"Cookie": "sid=abc"}
        )

    assert recorder.requests[0].headers.get("cookie") == "sid=abc"
    assert recorder.requests[1].headers.get("cookie") is None


async def test_proxy_authorization_stripped_on_cross_origin_redirect() -> None:
    recorder = _Recorder(
        [
            {"status_code": 302, "headers": {"Location": "https://evil/x"}},
            {"status_code": 200, "text": "ok"},
        ]
    )
    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(recorder),
        follow_redirects=True,
    ) as c:
        await c.get(
            "https://api.example.com/",
            headers={"Proxy-Authorization": "Basic x"},
        )

    assert recorder.requests[1].headers.get("proxy-authorization") is None


async def test_authorization_preserved_on_same_origin_redirect() -> None:
    recorder = _Recorder(
        [
            {
                "status_code": 302,
                "headers": {"Location": "https://api.example.com/next"},
            },
            {"status_code": 200, "text": "ok"},
        ]
    )
    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(recorder),
        follow_redirects=True,
    ) as c:
        await c.get(
            "https://api.example.com/first",
            headers={"Authorization": "Bearer s3cret"},
        )

    assert recorder.requests[1].headers.get("authorization") == "Bearer s3cret"


async def test_authorization_stripped_on_scheme_downgrade_same_host() -> None:
    # Same hostname, https → http: still considered a credential leak because
    # the credential would travel in cleartext.
    recorder = _Recorder(
        [
            {
                "status_code": 302,
                "headers": {"Location": "http://api.example.com/next"},
            },
            {"status_code": 200, "text": "ok"},
        ]
    )
    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(recorder),
        follow_redirects=True,
    ) as c:
        await c.get(
            "https://api.example.com/first",
            headers={"Authorization": "Bearer s3cret"},
        )

    assert recorder.requests[1].headers.get("authorization") is None


async def test_auth_helper_reapplies_credentials_same_origin() -> None:
    # Using a ``BasicAuth`` helper re-attaches the Authorization header on
    # every request (including the redirect target), so end-to-end behaviour
    # is not regressed for same-origin chains where credentials are OK.
    recorder = _Recorder(
        [
            {
                "status_code": 302,
                "headers": {"Location": "https://api.example.com/next"},
            },
            {"status_code": 200, "text": "ok"},
        ]
    )
    async with hyperhttp.Client(
        transport=hyperhttp.MockTransport(recorder),
        follow_redirects=True,
        auth=("alice", "hunter2"),
    ) as c:
        await c.get("https://api.example.com/first")

    # Both requests carry the Basic header because auth re-applies per send.
    assert recorder.requests[0].headers.get("authorization", "").startswith("Basic ")
    assert recorder.requests[1].headers.get("authorization", "").startswith("Basic ")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
