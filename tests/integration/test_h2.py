"""
HTTP/2 integration: one connection, many concurrent streams.

These tests require ``hypercorn`` to spin up a real H2 server. If it isn't
installed, they're skipped (the unit-level H2 behavior is exercised by the
rest of the suite against the H1 server).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Tuple

import pytest
import pytest_asyncio

hypercorn = pytest.importorskip("hypercorn")

import ssl

from aiohttp import web as aioweb
from hypercorn.asyncio import serve  # type: ignore
from hypercorn.config import Config  # type: ignore

import hyperhttp

from tests.integration.conftest import _make_tls_contexts, _pick_port


async def _asgi_app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    path = scope["path"]
    if path.startswith("/bytes/"):
        size = int(path.rsplit("/", 1)[-1])
        body = b"x" * size
    elif path == "/slow":
        await asyncio.sleep(0.1)
        body = b"ok"
    else:
        body = b"ok"
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/octet-stream")]})
    await send({"type": "http.response.body", "body": body})


@pytest_asyncio.fixture
async def h2_server() -> AsyncIterator[Tuple[str, ssl.SSLContext]]:
    server_ctx, client_ctx, _ = _make_tls_contexts(alpn=("h2",))
    # hypercorn takes a file path for cert/key; use a tempdir.
    import os
    import tempfile
    import trustme  # type: ignore

    ca = trustme.CA()
    srv_cert = ca.issue_cert("127.0.0.1", "localhost")
    with tempfile.TemporaryDirectory() as tdir:
        cert_pem = os.path.join(tdir, "cert.pem")
        key_pem = os.path.join(tdir, "key.pem")
        srv_cert.cert_chain_pems[0].write_to_path(cert_pem)
        srv_cert.private_key_pem.write_to_path(key_pem)

        port = _pick_port()
        config = Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.certfile = cert_pem
        config.keyfile = key_pem
        config.alpn_protocols = ["h2"]
        config.accesslog = None
        config.errorlog = None

        # Let the client trust the same CA.
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ca.configure_trust(client_ctx)
        client_ctx.set_alpn_protocols(["h2", "http/1.1"])

        shutdown = asyncio.Event()

        task = asyncio.create_task(
            serve(_asgi_app, config, shutdown_trigger=shutdown.wait)
        )

        # Wait until the bound port accepts connections.
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                break
            except OSError:
                await asyncio.sleep(0.02)

        try:
            yield (f"https://127.0.0.1:{port}", client_ctx)
        finally:
            shutdown.set()
            task.cancel()
            try:
                await task
            except BaseException:
                pass


async def test_h2_basic_get(h2_server: Tuple[str, ssl.SSLContext]) -> None:
    url, client_ctx = h2_server
    async with hyperhttp.Client(http2=True, ssl_context=client_ctx) as c:
        r = await c.get(f"{url}/bytes/32")
        assert r.status_code == 200
        assert r.http_version == "HTTP/2"
        assert await r.aread() == b"x" * 32


async def test_h2_concurrent_streams_single_connection(
    h2_server: Tuple[str, ssl.SSLContext],
) -> None:
    url, client_ctx = h2_server
    async with hyperhttp.Client(http2=True, ssl_context=client_ctx) as c:
        N = 50

        async def one(i: int) -> int:
            r = await c.get(f"{url}/bytes/64")
            body = await r.aread()
            assert len(body) == 64
            return i

        results = await asyncio.gather(*(one(i) for i in range(N)))
        assert sorted(results) == list(range(N))

        # Only a small number of TCP connections should have been opened.
        stats = c.get_pool_stats()
        total = sum(s["total_connections"] for s in stats.values())
        assert total <= 2, stats
