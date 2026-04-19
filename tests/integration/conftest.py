"""
Shared fixtures for the integration tests.

The suite spins up a real ``aiohttp`` server on loopback (both plain HTTP and
optionally TLS with ALPN) for the client to talk to. Heavier fixtures like a
full HTTP/2 server depend on ``hypercorn`` and are skipped if unavailable.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from typing import AsyncIterator, Optional, Tuple

import pytest
import pytest_asyncio
from aiohttp import web as aioweb


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _default_routes(app: aioweb.Application) -> None:
    async def ok(request: aioweb.Request) -> aioweb.Response:
        return aioweb.Response(text="ok")

    async def bytes_of(request: aioweb.Request) -> aioweb.Response:
        size = int(request.match_info["size"])
        return aioweb.Response(body=b"x" * size, content_type="application/octet-stream")

    async def echo_method(request: aioweb.Request) -> aioweb.Response:
        body = await request.read()
        return aioweb.json_response(
            {"method": request.method, "body": body.decode("latin-1")}
        )

    async def chunked(request: aioweb.Request) -> aioweb.StreamResponse:
        resp = aioweb.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for i in range(5):
            await resp.write(b"chunk%d\n" % i)
        await resp.write_eof()
        return resp

    async def gzip_body(request: aioweb.Request) -> aioweb.Response:
        import gzip

        payload = b"hello " * 200
        compressed = gzip.compress(payload)
        return aioweb.Response(
            body=compressed,
            headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"},
        )

    async def brotli_body(request: aioweb.Request) -> aioweb.Response:
        try:
            import brotli  # type: ignore
        except ImportError:
            return aioweb.Response(status=501, text="brotli not installed")
        payload = b"hello " * 200
        compressed = brotli.compress(payload)
        return aioweb.Response(
            body=compressed,
            headers={"Content-Encoding": "br", "Content-Type": "text/plain"},
        )

    async def redirect(request: aioweb.Request) -> aioweb.Response:
        status = int(request.match_info["status"])
        return aioweb.Response(status=status, headers={"Location": "/echo"})

    async def slow(request: aioweb.Request) -> aioweb.Response:
        delay = float(request.query.get("delay", "0.2"))
        await asyncio.sleep(delay)
        return aioweb.Response(text="ok")

    async def boom(request: aioweb.Request) -> aioweb.Response:
        return aioweb.Response(status=500, text="boom")

    app.router.add_get("/", ok)
    app.router.add_get("/bytes/{size}", bytes_of)
    app.router.add_route("*", "/echo", echo_method)
    app.router.add_get("/chunked", chunked)
    app.router.add_get("/gzip", gzip_body)
    app.router.add_get("/brotli", brotli_body)
    app.router.add_route("*", "/redirect/{status}", redirect)
    app.router.add_get("/slow", slow)
    app.router.add_get("/boom", boom)


@pytest_asyncio.fixture
async def http_server() -> AsyncIterator[str]:
    """Plain HTTP/1.1 aiohttp server on loopback. Yields the base URL."""
    app = aioweb.Application()
    await _default_routes(app)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def _make_tls_contexts(
    alpn: Tuple[str, ...] = ("h2", "http/1.1"),
) -> Tuple[ssl.SSLContext, ssl.SSLContext, str]:
    """Build (server_ctx, client_ctx, ca_pem_path) with a self-signed cert."""
    try:
        import trustme  # type: ignore
    except ImportError:
        pytest.skip("trustme not installed; install hyperhttp[test]")

    ca = trustme.CA()
    srv_cert = ca.issue_cert("127.0.0.1", "localhost")

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_cert.configure_cert(server_ctx)
    server_ctx.set_alpn_protocols(list(alpn))

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca.configure_trust(client_ctx)
    client_ctx.set_alpn_protocols(list(alpn))

    return server_ctx, client_ctx, "<inline>"


@pytest_asyncio.fixture
async def https_h1_server() -> AsyncIterator[Tuple[str, ssl.SSLContext]]:
    """HTTPS aiohttp server with ALPN advertising only http/1.1."""
    server_ctx, client_ctx, _ = _make_tls_contexts(alpn=("http/1.1",))
    app = aioweb.Application()
    await _default_routes(app)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port, ssl_context=server_ctx)
    await site.start()
    try:
        yield (f"https://127.0.0.1:{port}", client_ctx)
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def raw_tcp_server() -> AsyncIterator[Tuple[str, "asyncio.Queue[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]"]]:
    """A bare TCP server that pushes (reader, writer) pairs onto a queue.

    Used to craft malformed responses at the byte level — e.g. a framing
    conflict or deliberately-broken headers.
    """
    queue: "asyncio.Queue[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]" = asyncio.Queue()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await queue.put((reader, writer))

    port = _pick_port()
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        yield (f"http://127.0.0.1:{port}", queue)
    finally:
        server.close()
        await server.wait_closed()
