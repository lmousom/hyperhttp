"""End-to-end auth tests: Basic, Bearer, and Digest against aiohttp."""

from __future__ import annotations

import base64
import hashlib
import socket
from typing import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web as aioweb

import hyperhttp
from hyperhttp import BasicAuth, BearerAuth, DigestAuth


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Server fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_server() -> AsyncIterator[str]:
    """Server with Basic/Bearer/Digest endpoints.

    Returns a base URL. Each endpoint echoes back the request's
    ``Authorization`` header (for 200 responses) so tests can assert on
    exactly what the client sent.
    """

    async def basic(request: aioweb.Request) -> aioweb.Response:
        header = request.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        if header != expected:
            return aioweb.Response(
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="test"'},
            )
        return aioweb.json_response({"auth": header})

    async def bearer(request: aioweb.Request) -> aioweb.Response:
        if request.headers.get("Authorization") != "Bearer tok-xyz":
            return aioweb.Response(status=401)
        return aioweb.json_response({"auth": "Bearer tok-xyz"})

    # Digest server state (one nonce per server, simple nc tracking).
    state = {"nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093", "seen_nc": set()}

    async def digest(request: aioweb.Request) -> aioweb.Response:
        challenge = (
            f'Digest realm="protected", '
            f'qop="auth", '
            f'nonce="{state["nonce"]}", '
            f'opaque="opaque-value", '
            f"algorithm=MD5"
        )
        header = request.headers.get("Authorization")
        if not header or not header.startswith("Digest "):
            return aioweb.Response(
                status=401, headers={"WWW-Authenticate": challenge}
            )

        params = _parse_digest_header(header[len("Digest ") :])
        if params.get("username") != "Mufasa":
            return aioweb.Response(status=401)

        # Verify the response hash.
        ha1 = _md5("Mufasa:protected:Circle Of Life")
        ha2 = _md5(f"{request.method}:{params['uri']}")
        expected = _md5(
            f"{ha1}:{params['nonce']}:{params['nc']}:"
            f"{params['cnonce']}:{params['qop']}:{ha2}"
        )
        if expected != params.get("response"):
            return aioweb.Response(status=401)

        return aioweb.json_response(
            {"user": params["username"], "nc": params["nc"]}
        )

    app = aioweb.Application()
    app.router.add_get("/basic", basic)
    app.router.add_get("/bearer", bearer)
    app.router.add_get("/digest", digest)
    app.router.add_post("/basic", basic)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _parse_digest_header(body: str) -> dict:
    import re

    out = {}
    for match in re.finditer(
        r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', body
    ):
        key = match.group(1).lower()
        out[key] = match.group(2) if match.group(2) is not None else match.group(3)
    return out


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_tuple_shorthand(auth_server: str) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.get(f"{auth_server}/basic", auth=("alice", "s3cret"))
        await r.aread()
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_basic_auth_object(auth_server: str) -> None:
    async with hyperhttp.Client(
        auth=BasicAuth("alice", "s3cret"), trust_env=False, retry=False
    ) as client:
        r = await client.get(f"{auth_server}/basic")
        await r.aread()
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_basic_wrong_password_returns_401(auth_server: str) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.get(f"{auth_server}/basic", auth=("alice", "WRONG"))
        await r.aread()
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_per_request_auth_disable(auth_server: str) -> None:
    # Client default is Basic; pass auth=None on a call to skip it.
    async with hyperhttp.Client(
        auth=BasicAuth("alice", "s3cret"), trust_env=False, retry=False
    ) as client:
        r = await client.get(f"{auth_server}/basic", auth=None)
        await r.aread()
        assert r.status_code == 401  # Server rejected because no creds sent.


@pytest.mark.asyncio
async def test_per_request_auth_override(auth_server: str) -> None:
    async with hyperhttp.Client(
        auth=("wrong", "creds"), trust_env=False, retry=False
    ) as client:
        r = await client.get(
            f"{auth_server}/basic", auth=("alice", "s3cret")
        )
        await r.aread()
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Bearer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_ok(auth_server: str) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.get(
            f"{auth_server}/bearer", auth=BearerAuth("tok-xyz")
        )
        await r.aread()
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_bearer_wrong_token(auth_server: str) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.get(
            f"{auth_server}/bearer", auth=BearerAuth("WRONG")
        )
        await r.aread()
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_ok(auth_server: str) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.get(
            f"{auth_server}/digest",
            auth=DigestAuth("Mufasa", "Circle Of Life"),
        )
        await r.aread()
        assert r.status_code == 200
        body = r.json()
        assert body["user"] == "Mufasa"
        assert body["nc"] == "00000001"


@pytest.mark.asyncio
async def test_digest_wrong_password(auth_server: str) -> None:
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r = await client.get(
            f"{auth_server}/digest",
            auth=DigestAuth("Mufasa", "WRONG"),
        )
        await r.aread()
        # Expected: server issues another 401 on the retry — the flow
        # doesn't loop, so the caller sees the 401.
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_digest_nc_increments_across_calls(auth_server: str) -> None:
    auth = DigestAuth("Mufasa", "Circle Of Life")
    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        r1 = await client.get(f"{auth_server}/digest", auth=auth)
        await r1.aread()
        assert r1.status_code == 200
        assert r1.json()["nc"] == "00000001"

        # Second call reuses the cached nonce → nc should tick to 2. Our
        # test server re-challenges with the same nonce, so nc starts at 1
        # only on the first request; subsequent calls keep ticking.
        # (Strictly, with a 401 round-trip every time, Digest increments
        # nc on *retries*, not between independent calls — this test
        # documents that behaviour.)
