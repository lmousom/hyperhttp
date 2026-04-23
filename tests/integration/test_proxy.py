"""Integration tests for HTTP proxy support.

Spins up a minimal HTTP proxy that:

- Handles ``CONNECT host:port`` tunnelling by opening a plain TCP socket to
  the requested origin and shuttling bytes both directions.
- Handles ``GET http://host/path HTTP/1.1`` absolute-form requests by
  opening a fresh TCP connection to the origin and forwarding the request
  line (rewritten to origin form), copying the response back to the client.
- Optionally requires HTTP Basic auth on ``Proxy-Authorization``.

The proxy records every request it saw so tests can assert it was
actually traversed.
"""

from __future__ import annotations

import asyncio
import base64
import socket
import ssl
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional, Tuple

import pytest
import pytest_asyncio
from aiohttp import web as aioweb

import hyperhttp
from hyperhttp.exceptions import ProxyError


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class ProxyLog:
    connects: List[str] = field(default_factory=list)
    http_requests: List[Tuple[str, str]] = field(default_factory=list)  # (method, absolute url)
    auth_headers: List[Optional[str]] = field(default_factory=list)


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            return b""
        buf.extend(chunk)
        if len(buf) > 64 * 1024:
            raise RuntimeError("Proxy request headers too large")
    return bytes(buf)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def _run_proxy(
    log: ProxyLog,
    *,
    require_auth: Optional[str] = None,
) -> Tuple[str, asyncio.AbstractServer]:
    """Start a toy HTTP proxy. Returns ``("http://127.0.0.1:<port>", server)``."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await _read_headers(reader)
            if not raw:
                return
            head, _, _ = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if not lines:
                return
            request_line = lines[0]
            try:
                method, target, _ = request_line.split(" ", 2)
            except ValueError:
                return

            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()

            auth = headers.get("proxy-authorization")
            log.auth_headers.append(auth)
            if require_auth is not None and auth != require_auth:
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Proxy-Authenticate: Basic realm=\"proxy\"\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
                await writer.drain()
                writer.close()
                return

            if method == "CONNECT":
                log.connects.append(target)
                host, _, port = target.partition(":")
                try:
                    origin_reader, origin_writer = await asyncio.open_connection(
                        host, int(port)
                    )
                except Exception as exc:
                    writer.write(
                        f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
                        .encode()
                    )
                    await writer.drain()
                    writer.close()
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(
                    _pipe(reader, origin_writer),
                    _pipe(origin_reader, writer),
                    return_exceptions=True,
                )
                return

            # Absolute-form HTTP request.
            log.http_requests.append((method, target))
            from urllib.parse import urlparse

            parsed = urlparse(target)
            origin_host = parsed.hostname
            origin_port = parsed.port or 80
            origin_path = parsed.path or "/"
            if parsed.query:
                origin_path += "?" + parsed.query

            origin_reader, origin_writer = await asyncio.open_connection(
                origin_host, origin_port
            )
            # Rewrite the request line to origin-form and strip hop-by-hop
            # headers. Pass the rest through.
            new_lines = [f"{method} {origin_path} HTTP/1.1"]
            saw_host = False
            for line in lines[1:]:
                if not line:
                    continue
                low = line.lower()
                if low.startswith("proxy-") or low.startswith("connection:"):
                    continue
                if low.startswith("host:"):
                    saw_host = True
                new_lines.append(line)
            if not saw_host:
                new_lines.append(f"Host: {origin_host}:{origin_port}")
            new_head = ("\r\n".join(new_lines) + "\r\n\r\n").encode("latin-1")
            origin_writer.write(new_head)

            # Forward any body the client already sent past the headers.
            idx = raw.index(b"\r\n\r\n") + 4
            leftover = raw[idx:]
            if leftover:
                origin_writer.write(leftover)
            await origin_writer.drain()

            await asyncio.gather(
                _pipe(reader, origin_writer),
                _pipe(origin_reader, writer),
                return_exceptions=True,
            )
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    port = _pick_port()
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    return (f"http://127.0.0.1:{port}", server)


async def _ok_server() -> Tuple[str, aioweb.AppRunner]:
    async def ok(request: aioweb.Request) -> aioweb.Response:
        return aioweb.json_response({"path": str(request.rel_url), "host": request.host})

    async def echo(request: aioweb.Request) -> aioweb.Response:
        body = await request.read()
        return aioweb.json_response(
            {"method": request.method, "body": body.decode("latin-1")}
        )

    app = aioweb.Application()
    app.router.add_get("/hello", ok)
    app.router.add_route("*", "/echo", echo)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return (f"http://127.0.0.1:{port}", runner)


async def _tls_ok_server() -> Tuple[str, aioweb.AppRunner, ssl.SSLContext]:
    try:
        import trustme  # type: ignore
    except ImportError:
        pytest.skip("trustme not installed; install hyperhttp[test]")

    ca = trustme.CA()
    srv_cert = ca.issue_cert("127.0.0.1", "localhost")
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_cert.configure_cert(server_ctx)
    server_ctx.set_alpn_protocols(["http/1.1"])

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca.configure_trust(client_ctx)
    client_ctx.set_alpn_protocols(["http/1.1"])

    async def ok(request: aioweb.Request) -> aioweb.Response:
        return aioweb.json_response({"secure": True, "path": str(request.rel_url)})

    app = aioweb.Application()
    app.router.add_get("/hello", ok)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port, ssl_context=server_ctx)
    await site.start()
    return (f"https://127.0.0.1:{port}", runner, client_ctx)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def proxy_and_origin() -> AsyncIterator[Tuple[str, str, ProxyLog]]:
    log = ProxyLog()
    origin_url, origin = await _ok_server()
    proxy_url, server = await _run_proxy(log)
    try:
        yield (proxy_url, origin_url, log)
    finally:
        server.close()
        await server.wait_closed()
        await origin.cleanup()


@pytest_asyncio.fixture
async def proxy_and_tls_origin() -> AsyncIterator[Tuple[str, str, ssl.SSLContext, ProxyLog]]:
    log = ProxyLog()
    origin_url, origin, client_ctx = await _tls_ok_server()
    proxy_url, server = await _run_proxy(log)
    try:
        yield (proxy_url, origin_url, client_ctx, log)
    finally:
        server.close()
        await server.wait_closed()
        await origin.cleanup()


@pytest_asyncio.fixture
async def auth_proxy_and_origin() -> AsyncIterator[Tuple[str, str, ProxyLog]]:
    log = ProxyLog()
    origin_url, origin = await _ok_server()
    # "alice:secret" -> base64
    expected = "Basic " + base64.b64encode(b"alice:secret").decode()
    proxy_url, server = await _run_proxy(log, require_auth=expected)
    try:
        yield (proxy_url, origin_url, log)
    finally:
        server.close()
        await server.wait_closed()
        await origin.cleanup()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_via_http_proxy(proxy_and_origin) -> None:
    proxy_url, origin_url, log = proxy_and_origin
    async with hyperhttp.Client(proxies=proxy_url, trust_env=False) as client:
        r = await client.get(f"{origin_url}/hello")
        assert r.status_code == 200
        body = await r.aread()
        assert b'"path":' in body
    assert len(log.http_requests) == 1
    method, target = log.http_requests[0]
    assert method == "GET"
    assert target.startswith(origin_url + "/hello")
    assert log.connects == []


@pytest.mark.asyncio
async def test_https_via_http_proxy_connect(proxy_and_tls_origin) -> None:
    proxy_url, origin_url, client_ctx, log = proxy_and_tls_origin
    async with hyperhttp.Client(
        proxies=proxy_url,
        ssl_context=client_ctx,
        trust_env=False,
        http2=False,
    ) as client:
        r = await client.get(f"{origin_url}/hello")
        assert r.status_code == 200
        body = await r.aread()
        assert b'"secure": true' in body or b'"secure":true' in body
    assert len(log.connects) == 1
    host, _, port = log.connects[0].partition(":")
    assert host == "127.0.0.1"
    # Proxy never saw an HTTP request line — it was tunneled under TLS.
    assert log.http_requests == []


@pytest.mark.asyncio
async def test_proxy_basic_auth(auth_proxy_and_origin) -> None:
    proxy_url_noauth, origin_url, log = auth_proxy_and_origin
    # Inject credentials into the proxy URL passed to the client.
    host_port = proxy_url_noauth.split("://", 1)[1]
    authed = f"http://alice:secret@{host_port}"
    async with hyperhttp.Client(proxies=authed, trust_env=False) as client:
        r = await client.get(f"{origin_url}/hello")
        assert r.status_code == 200
        await r.aread()
    assert log.auth_headers and log.auth_headers[-1] == "Basic " + base64.b64encode(
        b"alice:secret"
    ).decode()


@pytest.mark.asyncio
async def test_proxy_auth_missing_returns_407(auth_proxy_and_origin) -> None:
    proxy_url_noauth, origin_url, log = auth_proxy_and_origin
    async with hyperhttp.Client(proxies=proxy_url_noauth, trust_env=False) as client:
        r = await client.get(f"{origin_url}/hello")
        # The proxy emits 407; it's a regular HTTP response on the HTTP path.
        assert r.status_code == 407
        await r.aread()


@pytest.mark.asyncio
async def test_proxy_refuses_https_connect_surfaces_proxy_error(
    proxy_and_tls_origin,
) -> None:
    proxy_url, origin_url, client_ctx, log = proxy_and_tls_origin
    # Point CONNECT at a port where nothing is listening → proxy returns 502.
    bad_origin = "https://127.0.0.1:1"
    async with hyperhttp.Client(
        proxies=proxy_url,
        ssl_context=client_ctx,
        trust_env=False,
        http2=False,
        retry=False,
    ) as client:
        with pytest.raises(ProxyError):
            await client.get(f"{bad_origin}/hello")


@pytest.mark.asyncio
async def test_no_proxy_env_bypasses(monkeypatch, proxy_and_origin) -> None:
    proxy_url, origin_url, log = proxy_and_origin
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    # Scope NO_PROXY to the origin host so the request goes direct.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    async with hyperhttp.Client(trust_env=True) as client:
        r = await client.get(f"{origin_url}/hello")
        assert r.status_code == 200
        await r.aread()
    assert log.http_requests == []
    assert log.connects == []


@pytest.mark.asyncio
async def test_env_proxy_used_when_trust_env(monkeypatch, proxy_and_origin) -> None:
    proxy_url, origin_url, log = proxy_and_origin
    # Clear any inherited proxy env, then set only HTTP_PROXY.
    for var in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    async with hyperhttp.Client(trust_env=True) as client:
        r = await client.get(f"{origin_url}/hello")
        assert r.status_code == 200
        await r.aread()
    assert len(log.http_requests) == 1
