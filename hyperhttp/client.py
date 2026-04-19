"""
HyperHTTP — high-performance async HTTP/1.1 + HTTP/2 client.

This module exposes the two public types developers interact with:

- ``Client``: an async context manager that holds a connection pool and a
  retry/circuit-breaker policy. Reuse one ``Client`` for the lifetime of
  your app/process.
- ``Response``: the response object returned by ``await client.request()``.

The hot path is built around streaming. ``Response`` lazily consumes a body
iterator from the transport so memory usage stays bounded for large
downloads. Convenience accessors (``aread``, ``json``, ``text``) are
materialization helpers on top of that stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType
from typing import (
    Any,
    AsyncIterator,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    Union,
)

from hyperhttp._compat import accept_encoding, json_dumps, json_loads
from hyperhttp._compression import IdentityDecoder, make_decoder
from hyperhttp._headers import Headers, HeadersInput
from hyperhttp._url import URL, QueryInput, encode_query
from hyperhttp.connection.pool import ConnectionPoolManager, PoolOptions
from hyperhttp.connection.transport import RawResponse, Transport
from hyperhttp.cookies import Cookies, CookiesInput
from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager
from hyperhttp.errors.retry import RetryHandler, RetryPolicy
from hyperhttp.errors.telemetry import ErrorTelemetry
from hyperhttp.exceptions import (
    HTTPStatusError,
    HyperHTTPError,
    InvalidURL,
    ReadTimeout,
    RemoteProtocolError,
    StreamConsumed,
    TooManyRedirects,
)
from hyperhttp.utils.buffer_pool import BufferPool
from hyperhttp.utils.dns_cache import DNSResolver

__version__ = "2.0.0"

logger = logging.getLogger("hyperhttp.client")

__all__ = ["Client", "Response", "Request", "__version__"]


_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Request value object
# ---------------------------------------------------------------------------


class Request:
    """A logical HTTP request (snapshot after URL+headers are normalized)."""

    __slots__ = ("method", "url", "headers", "content")

    def __init__(
        self,
        method: str,
        url: URL,
        headers: Headers,
        content: Any,
    ) -> None:
        self.method = method.upper()
        self.url = url
        self.headers = headers
        self.content = content

    def __repr__(self) -> str:
        return f"Request({self.method} {self.url})"


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class Response:
    """An HTTP response with a streaming body.

    The body is lazy. Use ``aiter_bytes()`` / ``aiter_text()`` /
    ``aiter_lines()`` for streaming, or call ``aread()`` once to materialize
    the body into ``content`` (after which ``text``/``json()`` are sync).

    Always either fully consume the response or close it via
    ``await response.aclose()`` (or ``async with`` form) so the underlying
    connection is returned to the pool.
    """

    __slots__ = (
        "status_code",
        "http_version",
        "headers",
        "url",
        "request",
        "elapsed",
        "_raw",
        "_decoder",
        "_iter",
        "_consumed",
        "_closed",
        "_content",
        "_default_encoding",
        "_on_close",
    )

    def __init__(
        self,
        *,
        raw: RawResponse,
        request: Request,
        url: URL,
        elapsed: float,
        default_encoding: str = "utf-8",
        on_close: Any = None,
    ) -> None:
        self.status_code = raw.status_code
        self.http_version = raw.http_version
        self.headers = raw.headers
        self.url = url
        self.request = request
        self.elapsed = elapsed
        self._raw = raw
        ce = raw.headers.get("content-encoding")
        self._decoder = make_decoder(ce)
        self._iter = raw.aiter_raw()
        self._consumed = False
        self._closed = False
        self._content: Optional[bytes] = None
        self._default_encoding = default_encoding
        self._on_close = on_close

    # -- context manager ---------------------------------------------------

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._raw.aclose()
        finally:
            if self._on_close is not None:
                cb = self._on_close
                self._on_close = None
                try:
                    await cb()
                except Exception:
                    logger.debug("Response on_close callback failed", exc_info=True)

    # -- streaming accessors -----------------------------------------------

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        """Yield raw (still-encoded) body chunks. Rare — most callers want ``aiter_bytes``."""
        if self._consumed:
            raise StreamConsumed("Response stream has already been consumed")
        self._consumed = True
        try:
            async for chunk in self._iter:
                if chunk:
                    yield chunk
        finally:
            await self.aclose()

    async def aiter_bytes(
        self, chunk_size: Optional[int] = None
    ) -> AsyncIterator[bytes]:
        """Yield decoded body chunks (after any Content-Encoding is stripped)."""
        if self._consumed:
            raise StreamConsumed("Response stream has already been consumed")
        self._consumed = True
        buffer = bytearray()
        try:
            async for raw in self._iter:
                if not raw:
                    continue
                decoded = self._decoder.decompress(raw)
                if not decoded:
                    continue
                if chunk_size is None:
                    yield decoded
                    continue
                buffer.extend(decoded)
                while len(buffer) >= chunk_size:
                    yield bytes(buffer[:chunk_size])
                    del buffer[:chunk_size]
            tail = self._decoder.flush()
            if tail:
                if chunk_size is None:
                    yield tail
                else:
                    buffer.extend(tail)
            if buffer:
                yield bytes(buffer)
        finally:
            await self.aclose()

    async def aiter_text(
        self,
        chunk_size: Optional[int] = None,
        encoding: Optional[str] = None,
    ) -> AsyncIterator[str]:
        enc = encoding or self.encoding
        async for chunk in self.aiter_bytes(chunk_size):
            yield chunk.decode(enc, errors="replace")

    async def aiter_lines(self, encoding: Optional[str] = None) -> AsyncIterator[str]:
        enc = encoding or self.encoding
        buffer = ""
        async for chunk in self.aiter_bytes():
            buffer += chunk.decode(enc, errors="replace")
            while True:
                nl = buffer.find("\n")
                if nl < 0:
                    break
                line = buffer[: nl + 1]
                buffer = buffer[nl + 1 :]
                yield line
        if buffer:
            yield buffer

    # -- bulk accessors ----------------------------------------------------

    async def aread(self) -> bytes:
        """Materialize the full body into memory and return it."""
        if self._content is not None:
            return self._content

        # Hot path for large bodies: Content-Length is known and the response
        # is identity-encoded (the common case — most servers only set a
        # Content-Encoding when the body is actually compressed). We collect
        # each raw chunk as a reference (zero copies) and let ``b"".join``
        # do a single memcpy pass into a freshly-allocated immutable bytes
        # object. That's one full-body memcpy total — half what either
        # ``bytearray.extend + bytes()`` or a pre-sized bytearray + slice
        # assignment would cost.
        cl_header = self.headers.get("content-length")
        if (
            cl_header is not None
            and not self._consumed
            and isinstance(self._decoder, IdentityDecoder)
        ):
            try:
                total = int(cl_header)
            except ValueError:
                total = -1
            if total >= 0:
                self._consumed = True
                try:
                    if total == 0:
                        self._content = b""
                        return self._content
                    first: Optional[bytes] = None
                    chunks: Optional[List[bytes]] = None
                    collected = 0
                    async for chunk in self._iter:
                        if not chunk:
                            continue
                        clen = len(chunk)
                        collected += clen
                        if collected > total:
                            raise RemoteProtocolError(
                                f"Server sent more bytes than Content-Length ({total})"
                            )
                        # Zero-copy single-chunk fast path: the whole body
                        # arrived in one piece — hand it straight back.
                        if first is None:
                            first = chunk
                            continue
                        if chunks is None:
                            chunks = [first]
                        chunks.append(chunk)
                    if collected != total:
                        raise RemoteProtocolError(
                            f"Body truncated: expected {total} bytes, got {collected}"
                        )
                    if chunks is not None:
                        self._content = b"".join(chunks)
                    elif first is not None:
                        self._content = first
                    else:
                        self._content = b""
                    return self._content
                finally:
                    await self.aclose()

        # Generic path: unknown length or a transforming decoder (gzip,
        # brotli, zstd). Collect raw decoded chunks and join once at the end.
        chunks_list: List[bytes] = []
        async for chunk in self.aiter_bytes():
            if chunk:
                chunks_list.append(chunk)
        self._content = b"".join(chunks_list)
        return self._content

    @property
    def content(self) -> bytes:
        if self._content is None:
            raise StreamConsumed(
                "Response body has not been read; call await response.aread() first"
            )
        return self._content

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")

    def json(self) -> Any:
        return json_loads(self.content)

    @property
    def encoding(self) -> str:
        ctype = self.headers.get("content-type", "")
        for piece in ctype.split(";"):
            piece = piece.strip().lower()
            if piece.startswith("charset="):
                return piece.split("=", 1)[1].strip().strip("\"'") or self._default_encoding
        return self._default_encoding

    # -- helpers -----------------------------------------------------------

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            msg = f"HTTP {self.status_code} for {self.request.method} {self.url}"
            raise HTTPStatusError(msg, request=self.request, response=self)

    def __repr__(self) -> str:
        return f"<Response [{self.status_code} {self.http_version}]>"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """Async HTTP client.

    Reuse one instance for the lifetime of your application — it owns the
    connection pool, DNS cache, retry handler, and circuit-breaker manager.

    Example:

        async with hyperhttp.Client() as client:
            response = await client.get("https://example.com")
            print(await response.aread())
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        headers: HeadersInput = None,
        cookies: CookiesInput = None,
        timeout: Optional[Union[float, "Timeout"]] = 30.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 120.0,
        http2: bool = True,
        verify: Any = True,
        cert: Any = None,
        ssl_context: Any = None,
        connect_timeout: Optional[float] = 10.0,
        happy_eyeballs_delay: float = 0.25,
        follow_redirects: bool = False,
        max_redirects: int = 20,
        retry: Optional[Union[RetryPolicy, bool]] = None,
        circuit_breaker_manager: Optional[DomainCircuitBreakerManager] = None,
        telemetry: Optional[ErrorTelemetry] = None,
        user_agent: Optional[str] = None,
        accept_compressed: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_timeout = _normalize_timeout(timeout)
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects
        self._closed = False

        # Default request headers.
        self._headers = Headers()
        self._headers.set("User-Agent", user_agent or f"hyperhttp/{__version__}")
        if accept_compressed:
            self._headers.set("Accept-Encoding", accept_encoding())
        self._headers.set("Accept", "*/*")
        if headers:
            self._headers.update(headers)

        self._cookies = Cookies(cookies)

        self._buffer_pool = BufferPool()
        self._dns = DNSResolver()
        self._pool = ConnectionPoolManager(
            options=PoolOptions(
                max_connections=max_connections,
                max_connections_per_host=max_keepalive_connections,
                keepalive_expiry=keepalive_expiry,
                http2=http2,
                verify=verify,
                cert=cert,
                ssl_context=ssl_context,
                connect_timeout=connect_timeout,
                happy_eyeballs_delay=happy_eyeballs_delay,
            ),
            dns=self._dns,
            buffer_pool=self._buffer_pool,
        )

        # Retry/circuit-breaker is opt-in; when neither is configured we skip
        # the wrapper entirely on the hot path (saves ~40us per request).
        if retry is False or (retry is None and circuit_breaker_manager is None and telemetry is None):
            self._retry_handler: Optional[RetryHandler] = None
        else:
            policy = retry if isinstance(retry, RetryPolicy) else None
            self._retry_handler = RetryHandler(
                retry_policy=policy,
                circuit_breaker_manager=circuit_breaker_manager,
                telemetry=telemetry,
            )

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._pool.aclose()

    @property
    def closed(self) -> bool:
        return self._closed

    def get_pool_stats(self) -> Dict[str, Dict[str, int]]:
        """Return a snapshot of per-host connection pool statistics."""
        return self._pool.stats()

    # -- HTTP method shortcuts --------------------------------------------

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Response:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> Response:
        return await self.request("OPTIONS", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Response:
        return await self.request("DELETE", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PATCH", url, **kwargs)

    # -- main entry point --------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: QueryInput = None,
        headers: HeadersInput = None,
        cookies: CookiesInput = None,
        content: Any = None,
        data: Any = None,
        json: Any = _UNSET,
        timeout: Any = _UNSET,
        follow_redirects: Any = _UNSET,
        retry: bool = True,
    ) -> Response:
        """Issue a single HTTP request and return the response.

        ``content`` may be ``bytes``, ``bytearray``, ``memoryview``, or an
        async iterable of bytes (for chunked uploads). ``json`` and ``data``
        are convenience encoders.
        """
        if self._closed:
            raise HyperHTTPError("Client has been closed")

        full_url = self._build_url(url, params)
        merged_headers = self._merge_headers(headers)
        body, content_type = self._encode_body(content, data, json)
        if content_type and "content-type" not in merged_headers:
            merged_headers.set("Content-Type", content_type)

        request = Request(method, full_url, merged_headers, body)
        # Request-scoped cookies + jar.
        self._cookies.add_to_request(full_url, merged_headers)
        if cookies is not None:
            Cookies(cookies).add_to_request(full_url, merged_headers)

        timeout_obj = self._default_timeout if timeout is _UNSET else _normalize_timeout(timeout)
        do_redirects = (
            self._follow_redirects if follow_redirects is _UNSET else bool(follow_redirects)
        )

        if retry and self._retry_handler is not None:
            async def executor(*, method: str, url: str, **_: Any) -> Response:
                return await self._send_single(request, timeout_obj)

            response = await self._retry_handler.execute(
                executor,
                method=request.method,
                url=str(request.url),
                domain=full_url.host_port,
            )
        else:
            response = await self._send_single(request, timeout_obj)

        if do_redirects and response.is_redirect:
            response = await self._handle_redirects(response, request, timeout_obj)

        return response

    async def stream(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Response:
        """Identical to ``request``; returned response can be iterated lazily."""
        return await self.request(method, url, **kwargs)

    # -- internals ---------------------------------------------------------

    def _build_url(self, url: str, params: QueryInput) -> URL:
        if self._base_url and not url.startswith(("http://", "https://")):
            full = f"{self._base_url}/{url.lstrip('/')}"
        else:
            full = url
        try:
            obj = URL(full)
        except InvalidURL:
            raise
        if params is not None:
            obj = obj.with_query(params)
        return obj

    def _merge_headers(self, request_headers: HeadersInput) -> Headers:
        merged = self._headers.copy()
        if request_headers is not None:
            merged.update(request_headers)
        return merged

    def _encode_body(
        self,
        content: Any,
        data: Any,
        json: Any,
    ) -> Tuple[Any, Optional[str]]:
        if json is not _UNSET:
            return json_dumps(json), "application/json"
        if data is not None:
            if isinstance(data, Mapping) or (
                isinstance(data, Iterable) and not isinstance(data, (str, bytes, bytearray))
            ):
                encoded = encode_query(data).encode("ascii")
                return encoded, "application/x-www-form-urlencoded"
            if isinstance(data, str):
                return data.encode("utf-8"), "text/plain; charset=utf-8"
            return data, None
        if content is None:
            return None, None
        if isinstance(content, str):
            return content.encode("utf-8"), "text/plain; charset=utf-8"
        return content, None

    async def _send_single(self, request: Request, timeout: "Timeout") -> Response:
        start = time.monotonic()
        transport = await self._pool.acquire(request.url, timeout=timeout.pool)

        # On any failure, make sure we release/discard the transport.
        raw: Optional[RawResponse] = None
        try:
            raw = await transport.handle_request(
                method=request.method,
                url=request.url,
                headers=request.headers,
                body=request.content,
                timeout=timeout.read,
            )
        except BaseException:
            self._pool.release(transport, discard=True)
            raise

        elapsed = time.monotonic() - start

        # H2 transports stay in the active set; release marks the slot free.
        # H1 transports are returned to the idle deque.
        async def _release_pool_slot() -> None:
            self._pool.release(transport)

        response = Response(
            raw=raw,
            request=request,
            url=request.url,
            elapsed=elapsed,
            on_close=_release_pool_slot,
        )
        # Extract response cookies as soon as the head is in.
        self._cookies.extract_from_response(request.url, response.headers)
        return response

    async def _handle_redirects(
        self,
        response: Response,
        request: Request,
        timeout: "Timeout",
    ) -> Response:
        history: List[Response] = []
        current = response

        for _ in range(self._max_redirects):
            if not current.is_redirect:
                return current

            location = current.headers.get("location")
            if not location:
                return current

            new_url = current.url.join(location)
            new_method, new_body = self._redirect_method_and_body(
                current.status_code, request.method, request.content
            )

            history.append(current)
            # Drain the previous response so its connection is released.
            try:
                await current.aclose()
            except Exception:
                pass

            new_headers = request.headers.copy()
            # Drop content-headers if body is being dropped.
            if new_body is None:
                for h in ("content-length", "content-type", "transfer-encoding"):
                    if h in new_headers:
                        del new_headers[h]

            new_request = Request(new_method, new_url, new_headers, new_body)
            self._cookies.add_to_request(new_url, new_headers)
            current = await self._send_single(new_request, timeout)
            request = new_request

        raise TooManyRedirects(
            f"Exceeded maximum redirect count ({self._max_redirects})"
        )

    @staticmethod
    def _redirect_method_and_body(
        status: int, method: str, body: Any
    ) -> Tuple[str, Any]:
        # RFC 7231: 301/302 historically downgrade non-GET to GET (matching
        # browser behavior). 303 always downgrades to GET. 307/308 must
        # preserve the method *and* body.
        if status in (307, 308):
            return method, body
        if status == 303:
            return "GET", None
        if status in (301, 302) and method not in ("GET", "HEAD"):
            return "GET", None
        return method, body


# ---------------------------------------------------------------------------
# Timeout value object
# ---------------------------------------------------------------------------


class Timeout:
    """Per-phase timeouts. Mirrors httpx's ``Timeout`` shape.

    A single float assigns the same value to every phase. ``None`` means no
    timeout for that phase.
    """

    __slots__ = ("connect", "read", "write", "pool")

    def __init__(
        self,
        timeout: Optional[float] = None,
        *,
        connect: Optional[float] = _UNSET,  # type: ignore[assignment]
        read: Optional[float] = _UNSET,  # type: ignore[assignment]
        write: Optional[float] = _UNSET,  # type: ignore[assignment]
        pool: Optional[float] = _UNSET,  # type: ignore[assignment]
    ) -> None:
        def _resolve(val: Any) -> Optional[float]:
            return timeout if val is _UNSET else val

        self.connect = _resolve(connect)
        self.read = _resolve(read)
        self.write = _resolve(write)
        self.pool = _resolve(pool)

    def __repr__(self) -> str:
        return (
            f"Timeout(connect={self.connect}, read={self.read}, "
            f"write={self.write}, pool={self.pool})"
        )


def _normalize_timeout(value: Any) -> Timeout:
    if isinstance(value, Timeout):
        return value
    return Timeout(value)
