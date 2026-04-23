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
import inspect
import logging
import time
from types import TracebackType
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
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
from hyperhttp._multipart import MultipartEncoder
from hyperhttp._proxy import ProxiesInput, ProxyConfig
from hyperhttp._url import URL, QueryInput, encode_query
from hyperhttp.auth import Auth, _coerce_auth
from hyperhttp.connection.pool import ConnectionPoolManager, PoolOptions
from hyperhttp.connection.transport import RawResponse, Transport  # noqa: F401
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
    ResponseTooLarge,
    StreamConsumed,
    TooManyRedirects,
)
from hyperhttp.utils.buffer_pool import BufferPool
from hyperhttp.utils.dns_cache import DNSResolver

__version__ = "2.1.0"

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
        "_max_response_size",
        "_raw_bytes_seen",
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
        max_response_size: Optional[int] = None,
        max_decompressed_size: Optional[int] = None,
    ) -> None:
        self.status_code = raw.status_code
        self.http_version = raw.http_version
        self.headers = raw.headers
        self.url = url
        self.request = request
        self.elapsed = elapsed
        self._raw = raw
        ce = raw.headers.get("content-encoding")
        # The decoder's cap covers both identity-encoded bodies (effectively
        # a raw-size cap) and compressed bodies (a zip-bomb cap). We always
        # feed bytes through the decoder, so this is the single chokepoint.
        self._decoder = make_decoder(ce, max_output_size=max_decompressed_size)
        self._iter = raw.aiter_raw()
        self._consumed = False
        self._closed = False
        self._content: Optional[bytes] = None
        self._default_encoding = default_encoding
        self._on_close = on_close
        self._max_response_size = max_response_size
        self._raw_bytes_seen = 0

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

    def _track_raw(self, size: int) -> None:
        """Accounting helper: enforce the raw-bytes-seen cap.

        ``max_response_size`` is the cap on *encoded* bytes off the wire —
        a gzip bomb is caught by the decoder's cap (:class:`Decoder`), this
        one catches attackers that don't bother compressing at all and just
        stream an endless identity body. Crossing it short-circuits the
        read so we never allocate the next chunk.
        """
        if self._max_response_size is None:
            return
        self._raw_bytes_seen += size
        if self._raw_bytes_seen > self._max_response_size:
            raise ResponseTooLarge(
                "Response body exceeded max_response_size "
                f"({self._max_response_size} bytes)"
            )

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        """Yield raw (still-encoded) body chunks. Rare — most callers want ``aiter_bytes``."""
        if self._consumed:
            raise StreamConsumed("Response stream has already been consumed")
        self._consumed = True
        try:
            async for chunk in self._iter:
                if chunk:
                    self._track_raw(len(chunk))
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
                self._track_raw(len(raw))
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
                # Enforce the raw-size cap before we even touch the wire:
                # a server that advertises Content-Length larger than our
                # cap is free to lie, but we won't allocate that memory.
                if (
                    self._max_response_size is not None
                    and total > self._max_response_size
                ):
                    await self.aclose()
                    raise ResponseTooLarge(
                        "Response Content-Length "
                        f"({total}) exceeds max_response_size "
                        f"({self._max_response_size} bytes)"
                    )
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
                        self._track_raw(clen)
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
        proxies: "ProxiesInput" = None,
        trust_env: bool = True,
        auth: Any = None,
        event_hooks: Optional[Mapping[str, Iterable[Callable[..., Any]]]] = None,
        transport: Any = None,
        max_response_size: Optional[int] = None,
        max_decompressed_size: Optional[int] = 64 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_timeout = _normalize_timeout(timeout)
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects
        self._closed = False

        # Surface a warning immediately when TLS verification is disabled so
        # the choice is visible to whoever configures the Client, not only
        # on the first HTTPS handshake (which may be much later).
        if verify is False and ssl_context is None:
            from hyperhttp.connection.tls import _warn_insecure_verify

            _warn_insecure_verify()
        # Resource limits applied to every response the client produces.
        # ``max_response_size`` caps raw bytes off the wire (catches attackers
        # that skip compression and stream forever). ``max_decompressed_size``
        # caps the output of any Content-Encoding decoder so a zip bomb can't
        # OOM the process — defaults to 64 MiB, override per-client for
        # bulk-download workloads or set to ``None`` to disable.
        if max_response_size is not None and max_response_size <= 0:
            raise ValueError("max_response_size must be positive or None")
        if max_decompressed_size is not None and max_decompressed_size <= 0:
            raise ValueError("max_decompressed_size must be positive or None")
        self._max_response_size = max_response_size
        self._max_decompressed_size = max_decompressed_size

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
        self._proxy_config = ProxyConfig(proxies, trust_env=trust_env)
        self._auth: Optional[Auth] = _coerce_auth(auth)
        # When ``transport`` is provided (typically a MockTransport or any
        # custom Transport implementation) the Client bypasses the
        # connection pool entirely and sends every request through it.
        # DNS, TLS and socket setup are skipped — perfect for tests and for
        # routing over exotic transports (e.g. in-process).
        self._transport: Optional[Transport] = transport
        # Public-facing so callers can append at runtime, matching the
        # ergonomics of ``httpx``. Unknown events raise on lookup, not here.
        self.event_hooks: Dict[str, List[Callable[..., Any]]] = {
            "request": [],
            "response": [],
        }
        if event_hooks:
            for event, hooks in event_hooks.items():
                if event not in self.event_hooks:
                    raise ValueError(
                        f"Unknown event hook: {event!r} "
                        f"(supported: {sorted(self.event_hooks)})"
                    )
                self.event_hooks[event] = list(hooks)
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
                proxy_config=self._proxy_config,
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
        if self._transport is not None:
            await self._transport.aclose()
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
        files: Any = None,
        json: Any = _UNSET,
        timeout: Any = _UNSET,
        follow_redirects: Any = _UNSET,
        retry: bool = True,
        auth: Any = _UNSET,
    ) -> Response:
        """Issue a single HTTP request and return the response.

        ``content`` may be ``bytes``, ``bytearray``, ``memoryview``, or an
        async iterable of bytes (for chunked uploads). ``json`` and ``data``
        are convenience encoders.

        ``files`` triggers ``multipart/form-data`` encoding. It accepts a
        dict or an iterable of ``(name, value)`` pairs; each ``value`` may
        be ``bytes``, a ``str``, a ``pathlib.Path``, an open binary file,
        a ``(filename, content[, content_type])`` tuple, or a
        :class:`hyperhttp.MultipartFile`. When ``files`` is provided,
        ``data`` is treated as the set of text fields to include in the
        same multipart body.
        """
        if self._closed:
            raise HyperHTTPError("Client has been closed")

        full_url = self._build_url(url, params)
        merged_headers = self._merge_headers(headers)
        body, content_type, content_length = self._encode_body(
            content, data, json, files
        )
        if content_type and "content-type" not in merged_headers:
            merged_headers.set("Content-Type", content_type)
        if content_length is not None and "content-length" not in merged_headers:
            merged_headers.set("Content-Length", str(content_length))

        request = Request(method, full_url, merged_headers, body)
        # Request-scoped cookies + jar.
        self._cookies.add_to_request(full_url, merged_headers)
        if cookies is not None:
            Cookies(cookies).add_to_request(full_url, merged_headers)

        timeout_obj = self._default_timeout if timeout is _UNSET else _normalize_timeout(timeout)
        do_redirects = (
            self._follow_redirects if follow_redirects is _UNSET else bool(follow_redirects)
        )

        # Per-request ``auth=`` explicitly overrides the client default.
        # ``auth=None`` *disables* the default; ``auth=_UNSET`` inherits it.
        effective_auth: Optional[Auth]
        if auth is _UNSET:
            effective_auth = self._auth
        else:
            effective_auth = _coerce_auth(auth)

        response = await self._send_with_auth(
            request, timeout_obj, effective_auth, retry=retry
        )

        if do_redirects and response.is_redirect:
            response = await self._handle_redirects(response, request, timeout_obj)

        return response

    async def _dispatch(
        self,
        request: Request,
        timeout_obj: "Timeout",
        *,
        retry: bool,
    ) -> Response:
        """Send a single Request with retry + circuit breaker wrapping.

        Event hooks fire inside each retry attempt: ``request`` hooks run
        immediately before the network write (so retries and auth rounds
        re-sign with the current clock), and ``response`` hooks run right
        after the response head is parsed.
        """
        if retry and self._retry_handler is not None:
            async def executor(*, method: str, url: str, **_: Any) -> Response:
                await self._fire_hooks("request", request)
                response = await self._send_single(request, timeout_obj)
                await self._fire_hooks("response", response)
                return response

            return await self._retry_handler.execute(
                executor,
                method=request.method,
                url=str(request.url),
                domain=request.url.host_port,
            )
        await self._fire_hooks("request", request)
        response = await self._send_single(request, timeout_obj)
        await self._fire_hooks("response", response)
        return response

    async def _fire_hooks(self, event: str, payload: Any) -> None:
        hooks = self.event_hooks.get(event)
        if not hooks:
            return
        for hook in hooks:
            result = hook(payload)
            if inspect.isawaitable(result):
                await result

    async def _send_with_auth(
        self,
        request: Request,
        timeout_obj: "Timeout",
        auth: Optional[Auth],
        *,
        retry: bool,
    ) -> Response:
        """Run ``auth.auth_flow`` over repeated dispatches.

        Each yielded request goes through the full retry + circuit-breaker
        wrapping. Intermediate responses (e.g. the 401 that triggers a
        Digest round-trip) are drained so their connection returns to the
        pool cleanly before the next attempt.
        """
        if auth is None:
            return await self._dispatch(request, timeout_obj, retry=retry)

        flow = auth.auth_flow(request)
        try:
            next_request = next(flow)
        except StopIteration:
            return await self._dispatch(request, timeout_obj, retry=retry)

        response: Optional[Response] = None
        while True:
            response = await self._dispatch(next_request, timeout_obj, retry=retry)
            try:
                next_request = flow.send(response)
            except StopIteration:
                return response
            # We'll replace ``response`` — drain the old one so the connection
            # can be reused. Swallow errors so auth isn't masked by a
            # transport hiccup during drain.
            try:
                await response.aread()
            except Exception:
                pass
            await response.aclose()

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
        files: Any = None,
    ) -> Tuple[Any, Optional[str], Optional[int]]:
        # Multipart: when ``files`` is set (or the caller hands us a
        # pre-built encoder) we return a streaming body with a known
        # Content-Length whenever possible.
        if files is not None or isinstance(content, MultipartEncoder) or isinstance(data, MultipartEncoder):
            encoder = self._build_multipart(content, data, files)
            return encoder, encoder.content_type, encoder.content_length
        if json is not _UNSET:
            payload = json_dumps(json)
            return payload, "application/json", len(payload)
        if data is not None:
            if isinstance(data, Mapping) or (
                isinstance(data, Iterable) and not isinstance(data, (str, bytes, bytearray))
            ):
                encoded = encode_query(data).encode("ascii")
                return encoded, "application/x-www-form-urlencoded", len(encoded)
            if isinstance(data, str):
                encoded = data.encode("utf-8")
                return encoded, "text/plain; charset=utf-8", len(encoded)
            return data, None, len(data) if isinstance(data, (bytes, bytearray, memoryview)) else None
        if content is None:
            return None, None, None
        if isinstance(content, str):
            encoded = content.encode("utf-8")
            return encoded, "text/plain; charset=utf-8", len(encoded)
        if isinstance(content, (bytes, bytearray, memoryview)):
            return content, None, len(content)
        return content, None, None

    @staticmethod
    def _build_multipart(content: Any, data: Any, files: Any) -> MultipartEncoder:
        # Pre-built encoder via ``content=`` or ``data=``.
        if isinstance(content, MultipartEncoder):
            return content
        if isinstance(data, MultipartEncoder):
            return data

        fields: list = []
        # Text fields from ``data``: accept a mapping or iterable of pairs.
        if data is not None:
            if isinstance(data, Mapping):
                fields.extend(data.items())
            elif isinstance(data, Iterable) and not isinstance(data, (str, bytes, bytearray)):
                fields.extend(data)
            else:
                raise TypeError(
                    "When used with files=, data must be a dict or iterable of pairs"
                )

        if files is None:
            return MultipartEncoder(fields)

        if isinstance(files, Mapping):
            raw_files = list(files.items())
        elif isinstance(files, Iterable):
            raw_files = list(files)
        else:
            raise TypeError("files must be a dict or iterable of (name, value) pairs")

        # In the ``files=`` context a bare ``str`` is conventionally a filesystem
        # path, not a text field. Wrap it as ``pathlib.Path`` so the encoder
        # picks the streaming-from-disk source.
        import pathlib

        for name, value in raw_files:
            if isinstance(value, str) and not isinstance(value, (bytes, bytearray)):
                value = pathlib.Path(value)
            fields.append((name, value))

        return MultipartEncoder(fields)

    async def _send_single(self, request: Request, timeout: "Timeout") -> Response:
        start = time.monotonic()

        # Injected transport (e.g. MockTransport) — no pool, no DNS, no TLS.
        if self._transport is not None:
            raw = await self._transport.handle_request(
                method=request.method,
                url=request.url,
                headers=request.headers,
                body=request.content,
                timeout=timeout.read,
            )
            response = Response(
                raw=raw,
                request=request,
                url=request.url,
                elapsed=time.monotonic() - start,
                max_response_size=self._max_response_size,
                max_decompressed_size=self._max_decompressed_size,
            )
            self._cookies.extract_from_response(request.url, response.headers)
            return response

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
            max_response_size=self._max_response_size,
            max_decompressed_size=self._max_decompressed_size,
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

            # Cross-origin / scheme-downgrade safety: never forward credentials
            # attached to the original request to a different origin, and never
            # downgrade credentials from https:// to http://. The cookie jar is
            # re-consulted below and applies its own Domain/Path/Secure rules.
            if _is_credential_leak_redirect(request.url, new_url):
                for h in ("authorization", "proxy-authorization", "cookie"):
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
# Redirect credential-leak safety
# ---------------------------------------------------------------------------


def _is_credential_leak_redirect(origin: URL, target: URL) -> bool:
    """Return ``True`` when forwarding auth from ``origin`` to ``target`` is unsafe.

    Two conditions trigger stripping: a cross-origin redirect (any of scheme,
    host, or port differs) and an https→http scheme downgrade (always unsafe,
    even if the hostname is unchanged — credentials would travel in clear).
    """
    if origin.scheme == "https" and target.scheme == "http":
        return True
    if origin.scheme != target.scheme:
        return True
    if origin.host.lower() != target.host.lower():
        return True
    if origin.port != target.port:
        return True
    return False


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
