"""
Transport abstraction.

A ``Transport`` wraps one TCP connection and knows how to serve one or more
HTTP requests over it. There are two implementations:

- ``H1Transport`` is single-request-at-a-time (HTTP/1.1).
- ``H2Transport`` is a multiplexing façade over a shared ``H2Connection``.

``connect()`` performs the TCP + TLS handshake, inspects ALPN on TLS
connections, and returns the appropriate transport. Plain ``http://`` always
yields ``H1Transport``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from typing import Any, AsyncIterator, List, Optional, Tuple

from hyperhttp._headers import Headers
from hyperhttp._proxy import ProxyURL
from hyperhttp._url import URL
from hyperhttp.connection._streams import FastStream, open_fast_stream, upgrade_to_tls
from hyperhttp.connection.tls import create_ssl_context
from hyperhttp.exceptions import (
    ConnectError,
    ConnectTimeout,
    LocalProtocolError,
    ProxyError,
    ReadError,
    ReadTimeout,
    RemoteProtocolError,
    TLSError,
    WriteError,
)
from hyperhttp.protocol.h1 import ResponseHead, build_request_head, make_parser
from hyperhttp.utils.buffer_pool import BufferPool
from hyperhttp.utils.dns_cache import (
    AddressInfo,
    DNSResolver,
    happy_eyeballs_connect,
)

logger = logging.getLogger("hyperhttp.connection.transport")

__all__ = [
    "Transport",
    "H1Transport",
    "H2Transport",
    "connect_transport",
    "RawResponse",
]


# ---------------------------------------------------------------------------
# Raw response record (before streaming body is wrapped into Response)
# ---------------------------------------------------------------------------


class RawResponse:
    """A response with a lazy async byte stream.

    The consumer is expected to iterate ``aiter_raw()`` to completion (or call
    ``aclose()``) to release the underlying connection.
    """

    __slots__ = ("status_code", "http_version", "headers", "_stream", "_closed", "_release_cb")

    def __init__(
        self,
        status_code: int,
        http_version: str,
        headers: Headers,
        stream: "AsyncIterator[bytes]",
        release_cb: "Optional[callable]" = None,  # type: ignore[valid-type]
    ) -> None:
        self.status_code = status_code
        self.http_version = http_version
        self.headers = headers
        self._stream = stream
        self._closed = False
        self._release_cb = release_cb

    def aiter_raw(self) -> "AsyncIterator[bytes]":
        return self._stream

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
        if self._release_cb is not None:
            try:
                await self._release_cb()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Transport base
# ---------------------------------------------------------------------------


class Transport:
    """Interface every transport must implement."""

    http_version: str = "HTTP/1.1"

    async def handle_request(
        self,
        *,
        method: str,
        url: URL,
        headers: Headers,
        body: Any,
        timeout: Optional[float],
    ) -> RawResponse:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    @property
    def closed(self) -> bool:
        raise NotImplementedError

    @property
    def reusable(self) -> bool:
        raise NotImplementedError

    @property
    def in_flight(self) -> int:
        """Number of concurrent requests currently being served."""
        return 0

    @property
    def max_concurrent(self) -> int:
        """Max concurrent requests this transport can serve."""
        return 1

    @property
    def host_port(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP/1.1 transport
# ---------------------------------------------------------------------------


class H1Transport(Transport):
    """One TCP connection, one in-flight HTTP/1.1 request at a time."""

    http_version = "HTTP/1.1"

    def __init__(
        self,
        stream: "FastStream",
        host_port: str,
        *,
        buffer_pool: Optional[BufferPool] = None,
        proxy_absolute_form: bool = False,
        proxy_auth_header: Optional[str] = None,
    ) -> None:
        self._stream = stream
        self._host_port = host_port
        self._buffer_pool = buffer_pool
        self._closed = False
        self._in_flight = 0
        self._keep_alive = True
        self._created_at = time.monotonic()
        self._last_used = self._created_at
        self._request_count = 0
        # When True, emit absolute-form request URIs ("GET http://host/path ..."),
        # as required when tunnelling plain HTTP through an HTTP proxy.
        self._proxy_absolute_form = proxy_absolute_form
        self._proxy_auth_header = proxy_auth_header

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reusable(self) -> bool:
        return (
            not self._closed
            and self._keep_alive
            and self._in_flight == 0
            and self._request_count < 1000  # recycle after 1000 reqs
        )

    @property
    def host_port(self) -> str:
        return self._host_port

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def handle_request(
        self,
        *,
        method: str,
        url: URL,
        headers: Headers,
        body: Any,
        timeout: Optional[float],
    ) -> RawResponse:
        if self._closed:
            raise ConnectError("Transport is closed")
        if self._in_flight:
            raise RuntimeError("H1Transport is already serving a request")
        self._in_flight = 1
        self._request_count += 1
        try:
            await self._send(method, url, headers, body)
            head, stream = await self._receive(method, timeout)
            return RawResponse(
                status_code=head.status_code,
                http_version=head.http_version,
                headers=head.headers,
                stream=stream,
                release_cb=self._request_done,
            )
        except BaseException:
            self._in_flight = 0
            self._keep_alive = False
            await self._hard_close()
            raise

    async def _request_done(self) -> None:
        self._in_flight = 0
        self._last_used = time.monotonic()
        if not self._keep_alive:
            await self._hard_close()

    async def _send(
        self,
        method: str,
        url: URL,
        headers: Headers,
        body: Any,
    ) -> None:
        # Decide framing for the request body.
        body_bytes: Optional[bytes] = None
        chunked = False
        content_length: Optional[int] = None

        if body is None:
            content_length = 0 if method in ("POST", "PUT", "PATCH") and headers.get("content-length") is None else None
            # Only emit Content-Length: 0 for methods that conventionally carry a body
            # if no explicit framing is set.
            if method in ("POST", "PUT", "PATCH"):
                content_length = 0
        elif isinstance(body, (bytes, bytearray, memoryview)):
            body_bytes = bytes(body)
            content_length = len(body_bytes)
        else:
            # Async iterable. If the caller (or the multipart encoder wiring
            # in client.py) knows the size, we honour Content-Length framing
            # and skip chunked encoding entirely — many servers reject
            # chunked uploads and it costs extra syscalls per chunk.
            explicit_cl = headers.get("content-length")
            if explicit_cl is not None:
                try:
                    content_length = int(explicit_cl)
                except ValueError:
                    content_length = None
            declared = getattr(body, "content_length", None)
            if content_length is None and isinstance(declared, int) and declared >= 0:
                content_length = declared
            if content_length is None:
                chunked = True

        if self._proxy_absolute_form:
            # RFC 7230 §5.3.2 absolute-form: "GET http://host/path HTTP/1.1".
            # We're speaking to the proxy; Proxy-Authorization is hop-by-hop
            # and belongs on *this* hop. Strip any user-supplied value so the
            # credential source is deterministic (the proxy URL), then attach
            # the configured one if present.
            target = f"{url.scheme}://{url.host_port}{url.target}"
            if (
                self._proxy_auth_header is not None
                or headers.get("proxy-authorization") is not None
            ):
                headers = headers.copy()
                if "proxy-authorization" in headers:
                    del headers["proxy-authorization"]
                if self._proxy_auth_header is not None:
                    headers["proxy-authorization"] = self._proxy_auth_header
        else:
            # Origin-form: we're either talking directly to the origin or
            # we're inside a CONNECT tunnel. Proxy-Authorization MUST NOT
            # be forwarded to the origin (RFC 7235 §4.4: it's hop-by-hop)
            # — leaking it would hand the proxy credential to the server.
            if headers.get("proxy-authorization") is not None:
                headers = headers.copy()
                del headers["proxy-authorization"]
            target = url.target

        head = build_request_head(
            method,
            target,
            url.authority,
            headers,
            content_length=content_length,
            chunked=chunked,
        )
        try:
            self._stream.write(head)
            if body_bytes is not None:
                self._stream.write(body_bytes)
                await self._stream.drain()
            elif chunked:
                async for chunk in body:
                    if not chunk:
                        continue
                    data = bytes(chunk)
                    self._stream.write(f"{len(data):x}\r\n".encode("ascii"))
                    self._stream.write(data)
                    self._stream.write(b"\r\n")
                    await self._stream.drain()
                self._stream.write(b"0\r\n\r\n")
                await self._stream.drain()
            elif content_length is not None and body is not None and body_bytes is None:
                # Sized streaming body: write chunks straight through with no
                # framing overhead. ``drain()`` once per chunk keeps socket
                # buffers bounded under backpressure.
                sent = 0
                async for chunk in body:
                    if not chunk:
                        continue
                    data = chunk if isinstance(chunk, (bytes, bytearray, memoryview)) else bytes(chunk)
                    if not isinstance(data, bytes):
                        data = bytes(data)
                    sent += len(data)
                    if sent > content_length:
                        raise LocalProtocolError(
                            f"Request body exceeded declared Content-Length "
                            f"({sent} > {content_length})"
                        )
                    self._stream.write(data)
                    await self._stream.drain()
                if sent != content_length:
                    raise LocalProtocolError(
                        f"Request body shorter than declared Content-Length "
                        f"({sent} < {content_length})"
                    )
            else:
                await self._stream.drain()
        except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
            raise
        except OSError as exc:
            raise WriteError(f"Write failed: {exc}") from exc

    async def _receive(
        self, method: str, timeout: Optional[float]
    ) -> Tuple[ResponseHead, AsyncIterator[bytes]]:
        parser = make_parser()
        head_request = method.upper() == "HEAD"
        deadline = time.monotonic() + timeout if timeout else None

        head: Optional[ResponseHead] = None
        pending_body: List[bytes] = []
        body_done = False

        # Pump the parser until we have a head. Bodies that came along in the
        # same packet are stashed into ``pending_body``.
        while head is None:
            try:
                data = await _recv(self._stream, deadline)
            except asyncio.TimeoutError as exc:
                raise ReadTimeout("Timed out waiting for response head") from exc
            if not data:
                raise RemoteProtocolError(
                    "Connection closed before response head was received"
                )
            for event in parser.feed(data):
                if isinstance(event, ResponseHead):
                    head = event
                    if head_request and hasattr(parser, "mark_no_body"):
                        parser.mark_no_body()  # type: ignore[attr-defined]
                elif event is None:
                    body_done = True
                elif event:  # non-empty bytes
                    pending_body.append(event)

        stream = self._stream  # local alias — avoids per-iter attribute lookup

        async def body_iter() -> "AsyncIterator[bytes]":
            for chunk in pending_body:
                yield chunk
            if body_done:
                self._keep_alive = parser.keep_alive
                return
            while True:
                try:
                    data = await _recv(stream, deadline)
                except asyncio.TimeoutError as exc:
                    raise ReadTimeout("Timed out while reading body") from exc
                if not data:
                    for event in parser.feed_eof():
                        if event is None:
                            self._keep_alive = False
                            return
                        if event:
                            yield event
                    return
                for event in parser.feed(data):
                    if event is None:
                        self._keep_alive = parser.keep_alive
                        return
                    if event:
                        yield event

        assert head is not None
        return head, body_iter()

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._hard_close()

    async def _hard_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
            try:
                await self._stream.wait_closed()
            except (BrokenPipeError, ConnectionError, asyncio.CancelledError):
                pass
        except Exception:
            pass


async def _recv(stream: "FastStream", deadline: Optional[float]) -> bytes:
    """Zero-copy read: return the next raw chunk from the protocol queue.

    Honors an absolute deadline if one was set on the request. The returned
    bytes object is the one the event loop handed us directly — no
    intermediate buffers or copies.
    """
    if deadline is None:
        return await stream.recv()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    return await asyncio.wait_for(stream.recv(), timeout=remaining)


# ---------------------------------------------------------------------------
# HTTP/2 transport (imported lazily)
# ---------------------------------------------------------------------------


def _import_h2():
    from hyperhttp.protocol.h2_mux import H2Transport as _H2  # noqa: WPS433 (deferred)

    return _H2


# ---------------------------------------------------------------------------
# Connect + ALPN dispatch
# ---------------------------------------------------------------------------


async def connect_transport(
    url: URL,
    *,
    dns: DNSResolver,
    ssl_context: Optional[ssl.SSLContext] = None,
    verify: Any = True,
    cert: Any = None,
    alpn_protocols: Tuple[str, ...] = ("h2", "http/1.1"),
    connect_timeout: Optional[float] = 10.0,
    happy_eyeballs_delay: float = 0.25,
    buffer_pool: Optional[BufferPool] = None,
    enable_http2: bool = True,
    proxy: Optional[ProxyURL] = None,
) -> Transport:
    """Open a transport to ``url``, negotiating ALPN on TLS.

    When ``proxy`` is given, connections are routed through the proxy:

    - ``http://`` target + ``http(s)://`` proxy: a single TCP/TLS connection to
      the proxy; requests use absolute-form URIs. No HTTP/2.
    - ``https://`` target + ``http(s)://`` proxy: ``CONNECT`` tunnel, then TLS
      to the origin inside the tunnel; ALPN is honoured as usual.
    """
    if proxy is not None:
        return await _connect_via_proxy(
            url,
            proxy=proxy,
            dns=dns,
            ssl_context=ssl_context,
            verify=verify,
            cert=cert,
            alpn_protocols=alpn_protocols,
            connect_timeout=connect_timeout,
            happy_eyeballs_delay=happy_eyeballs_delay,
            buffer_pool=buffer_pool,
            enable_http2=enable_http2,
        )

    addresses = await dns.resolve(url.host, url.port)
    if url.is_secure:
        ctx = ssl_context or create_ssl_context(
            verify=verify,
            cert=cert,
            alpn_protocols=alpn_protocols if enable_http2 else ("http/1.1",),
        )
    else:
        ctx = None

    async def factory(addr: AddressInfo):
        return await open_fast_stream(
            host=addr.sockaddr[0],
            port=url.port,
            ssl_context=ctx,
            server_hostname=url.host if ctx else None,
            timeout=connect_timeout,
        )

    stream: FastStream = await happy_eyeballs_connect(
        addresses,
        factory,
        stagger=happy_eyeballs_delay,
        timeout=connect_timeout,
    )

    selected = ""
    if ctx is not None:
        ssl_obj = stream.get_extra_info("ssl_object")
        if ssl_obj is not None:
            try:
                selected = ssl_obj.selected_alpn_protocol() or ""
            except Exception:
                selected = ""

    host_port = url.host_port
    if selected == "h2" and enable_http2:
        H2Transport = _import_h2()
        transport: Transport = H2Transport(
            reader=stream,
            writer=stream,
            host_port=host_port,
            authority=url.authority,
            scheme=url.scheme,
        )
        await transport.initialize()  # type: ignore[attr-defined]
        return transport

    return H1Transport(stream=stream, host_port=host_port, buffer_pool=buffer_pool)


# ---------------------------------------------------------------------------
# Proxy support (HTTP and HTTPS via HTTP/HTTPS proxy)
# ---------------------------------------------------------------------------


async def _connect_via_proxy(
    url: URL,
    *,
    proxy: ProxyURL,
    dns: DNSResolver,
    ssl_context: Optional[ssl.SSLContext],
    verify: Any,
    cert: Any,
    alpn_protocols: Tuple[str, ...],
    connect_timeout: Optional[float],
    happy_eyeballs_delay: float,
    buffer_pool: Optional[BufferPool],
    enable_http2: bool,
) -> Transport:
    # 1) TCP (+ optional TLS) to the proxy itself.
    proxy_addresses = await dns.resolve(proxy.host, proxy.port)
    proxy_ctx: Optional[ssl.SSLContext] = None
    if proxy.is_secure:
        # The proxy itself is reached over TLS; don't advertise h2 — proxies
        # that multiplex h2 over the hop to origin are out of scope.
        proxy_ctx = create_ssl_context(
            verify=verify,
            cert=cert,
            alpn_protocols=("http/1.1",),
        )

    async def proxy_factory(addr: AddressInfo):
        return await open_fast_stream(
            host=addr.sockaddr[0],
            port=proxy.port,
            ssl_context=proxy_ctx,
            server_hostname=proxy.host if proxy_ctx else None,
            timeout=connect_timeout,
        )

    stream: FastStream = await happy_eyeballs_connect(
        proxy_addresses,
        proxy_factory,
        stagger=happy_eyeballs_delay,
        timeout=connect_timeout,
    )

    host_port = url.host_port

    # 2a) Plain http:// target — no CONNECT tunnel; just issue absolute-form
    # requests over the proxy hop.
    if not url.is_secure:
        return H1Transport(
            stream=stream,
            host_port=host_port,
            buffer_pool=buffer_pool,
            proxy_absolute_form=True,
            proxy_auth_header=proxy.basic_auth_header(),
        )

    # 2b) https:// target — send CONNECT, then TLS inside the tunnel.
    try:
        await _send_connect_request(stream, url, proxy=proxy, timeout=connect_timeout)
        ctx = ssl_context or create_ssl_context(
            verify=verify,
            cert=cert,
            alpn_protocols=alpn_protocols if enable_http2 else ("http/1.1",),
        )
        await upgrade_to_tls(
            stream,
            ssl_context=ctx,
            server_hostname=url.host,
            timeout=connect_timeout,
        )
    except BaseException:
        stream.close()
        try:
            await stream.wait_closed()
        except Exception:
            pass
        raise

    selected = ""
    ssl_obj = stream.get_extra_info("ssl_object")
    if ssl_obj is not None:
        try:
            selected = ssl_obj.selected_alpn_protocol() or ""
        except Exception:
            selected = ""

    if selected == "h2" and enable_http2:
        H2Transport = _import_h2()
        transport: Transport = H2Transport(
            reader=stream,
            writer=stream,
            host_port=host_port,
            authority=url.authority,
            scheme=url.scheme,
        )
        await transport.initialize()  # type: ignore[attr-defined]
        return transport

    return H1Transport(stream=stream, host_port=host_port, buffer_pool=buffer_pool)


async def _send_connect_request(
    stream: "FastStream",
    url: URL,
    *,
    proxy: ProxyURL,
    timeout: Optional[float],
) -> None:
    """Send an HTTP CONNECT request and parse the response.

    Raises :class:`ProxyError` if the proxy refuses the tunnel.
    """
    target = f"{url.host}:{url.port}"
    lines = [
        f"CONNECT {target} HTTP/1.1",
        f"Host: {target}",
        "Proxy-Connection: keep-alive",
    ]
    auth = proxy.basic_auth_header()
    if auth is not None:
        lines.append(f"Proxy-Authorization: {auth}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    try:
        stream.write(request)
        await stream.drain()
    except (ConnectionError, BrokenPipeError) as exc:
        raise ProxyError(f"Failed to send CONNECT to proxy: {exc}") from exc
    except OSError as exc:
        raise ProxyError(f"Failed to send CONNECT to proxy: {exc}") from exc

    # Read until we see the end of headers. CONNECT 2xx responses have no
    # body, so we must not over-read past ``\r\n\r\n``.
    buf = bytearray()
    deadline = time.monotonic() + timeout if timeout else None
    while b"\r\n\r\n" not in buf:
        try:
            chunk = await _recv(stream, deadline)
        except asyncio.TimeoutError as exc:
            raise ProxyError("Timed out waiting for proxy CONNECT response") from exc
        if not chunk:
            raise ProxyError("Proxy closed connection during CONNECT")
        buf.extend(chunk)
        if len(buf) > 64 * 1024:
            raise ProxyError("Proxy CONNECT response headers exceed 64 KiB")

    header_end = buf.index(b"\r\n\r\n") + 4
    header_block = bytes(buf[:header_end])
    leftover = bytes(buf[header_end:])
    if leftover:
        # Misbehaving proxy — would corrupt TLS ClientHello. Reject.
        raise ProxyError(
            "Proxy sent unexpected bytes after CONNECT response headers"
        )

    status_line, _, _ = header_block.partition(b"\r\n")
    try:
        parts = status_line.split(b" ", 2)
        status_code = int(parts[1])
    except (IndexError, ValueError) as exc:
        raise ProxyError(
            f"Unparseable proxy CONNECT status line: {status_line!r}"
        ) from exc

    if not 200 <= status_code < 300:
        reason = parts[2].decode("latin-1", "replace") if len(parts) > 2 else ""
        raise ProxyError(
            f"Proxy refused CONNECT tunnel: {status_code} {reason}".strip()
        )
