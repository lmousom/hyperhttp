"""
In-memory transport for tests.

``MockTransport`` is a drop-in replacement for the real network transport.
It accepts a handler (sync or async, a list of canned responses, or a single
response) and plugs straight into the ``Client``:

    from hyperhttp import Client, MockTransport, MockResponse

    def handler(request):
        return MockResponse(200, json={"ok": True})

    async with Client(transport=MockTransport(handler)) as client:
        r = await client.get("https://api.example.com/things")
        assert r.json() == {"ok": True}

All retry / auth / event-hook / cookie / redirect machinery in ``Client``
runs unchanged against the mock — which makes tests both fast and realistic.

Design goals:

* **Zero-network, zero-threads, zero-sockets.** Pure Python.
* **Friendly handlers.** Sync or async, returning either a ``MockResponse``,
  a plain ``int`` (shortcut for ``MockResponse(status)``), or raising any
  ``hyperhttp`` exception to simulate transport-level failures.
* **Realistic wire semantics.** Bodies flow through ``RawResponse`` so
  ``aiter_bytes`` / ``aread`` / ``aclose`` behave exactly as in production.
* **Rich assertion API.** ``mock.calls``, ``mock.last_request``,
  ``mock.call_count``, and ``mock.reset()``.

Use it as the foundation for higher-level helpers (e.g. a custom ``Router``)
in your own test suite.
"""

from __future__ import annotations

import inspect
from typing import (
    Any,
    AsyncIterator,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from hyperhttp._compat import json_dumps
from hyperhttp._headers import Headers, HeadersInput
from hyperhttp._url import URL
from hyperhttp.connection.transport import RawResponse, Transport

__all__ = ["MockResponse", "MockTransport", "Router"]

_UNSET: Any = object()

# Any callable (sync or async) taking a Request and returning something
# resolvable to a MockResponse / int / already-raised exception.
Handler = Callable[..., Any]
# Anything a handler may return.
HandlerReturn = Union["MockResponse", int, "Response"]  # noqa: F821 (forward ref in docstring only)


# ---------------------------------------------------------------------------
# MockResponse — fluent response spec
# ---------------------------------------------------------------------------


class MockResponse:
    """A lightweight response spec used by ``MockTransport`` handlers.

    You can pick *one* of ``content``, ``text``, ``json`` and ``stream``.
    Passing more than one raises ``ValueError`` — there's no good way to
    reconcile multiple bodies.

    Parameters
    ----------
    status_code:
        HTTP status code, e.g. ``200``, ``404``.
    headers:
        Any headers-like input (dict, list of pairs, ``Headers``).
    content:
        Raw bytes body. Content-Length is set automatically.
    text:
        Unicode body; encoded as UTF-8. Content-Type defaults to
        ``text/plain; charset=utf-8``.
    json:
        Any JSON-serialisable object. Content-Type defaults to
        ``application/json``. Use the sentinel ``MockResponse.NO_BODY``
        (the default) to distinguish "no JSON" from ``json=None``.
    stream:
        Async iterable yielding bytes. Use this to simulate streamed or
        chunked-encoded bodies. Content-Length is *not* auto-computed.
    http_version:
        e.g. ``"HTTP/1.1"`` or ``"HTTP/2"``.
    """

    __slots__ = (
        "status_code",
        "http_version",
        "_headers",
        "_body",
        "_stream",
    )

    def __init__(
        self,
        status_code: int = 200,
        *,
        headers: HeadersInput = None,
        content: Optional[Union[bytes, bytearray, memoryview]] = None,
        text: Optional[str] = None,
        json: Any = _UNSET,
        stream: Optional[AsyncIterator[bytes]] = None,
        http_version: str = "HTTP/1.1",
    ) -> None:
        self.status_code = int(status_code)
        self.http_version = http_version

        body: Optional[bytes] = None
        default_content_type: Optional[str] = None

        body_sources = sum(
            x is not None and x is not _UNSET
            for x in (content, text, stream)
        ) + (1 if json is not _UNSET else 0)
        if body_sources > 1:
            raise ValueError(
                "MockResponse accepts at most one of content=/text=/json=/stream="
            )

        if content is not None:
            body = bytes(content)
        elif text is not None:
            body = text.encode("utf-8")
            default_content_type = "text/plain; charset=utf-8"
        elif json is not _UNSET:
            body = json_dumps(json)
            default_content_type = "application/json"

        self._body = body
        self._stream = stream

        hdrs = Headers(headers) if headers is not None else Headers()
        if body is not None and "content-length" not in hdrs:
            hdrs["content-length"] = str(len(body))
        if default_content_type and "content-type" not in hdrs:
            hdrs["content-type"] = default_content_type
        self._headers = hdrs

    def __repr__(self) -> str:
        return f"MockResponse({self.status_code})"

    def _build_raw(self) -> RawResponse:
        if self._stream is not None:
            stream: AsyncIterator[bytes] = self._stream
        else:
            body = self._body if self._body is not None else b""
            stream = _single_chunk_stream(body)
        return RawResponse(
            status_code=self.status_code,
            http_version=self.http_version,
            headers=self._headers,
            stream=stream,
        )


async def _single_chunk_stream(body: bytes) -> AsyncIterator[bytes]:
    if body:
        yield body


# ---------------------------------------------------------------------------
# MockTransport
# ---------------------------------------------------------------------------


class MockTransport(Transport):
    """In-memory transport for tests.

    Pass **any** of:

    * a **callable** ``handler(request) -> MockResponse`` (sync or async);
    * a **single** ``MockResponse`` — every request gets the same reply;
    * a **sequence** ``[MockResponse, ...]`` — popped left-to-right per
      call. Runs out → ``IndexError`` (loud failure beats silent reuse);
    * a **sequence that cycles** — wrap it with ``itertools.cycle`` first
      if you want repeats.

    A handler may also raise any ``hyperhttp`` exception (or ``OSError``)
    to simulate transport-level failures, which flows through the client's
    retry / circuit-breaker machinery exactly as the real thing would.
    """

    http_version = "HTTP/1.1"

    def __init__(
        self,
        handler: Union[
            Handler,
            MockResponse,
            Sequence[MockResponse],
            Mapping[str, MockResponse],
        ],
    ) -> None:
        self._handler = _normalise_handler(handler)
        self._calls: List[Any] = []
        self._closed = False

    # -- assertion API -----------------------------------------------------

    @property
    def calls(self) -> List[Any]:
        """List of every ``Request`` this transport has served (FIFO)."""
        return self._calls

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def last_request(self) -> Optional[Any]:
        return self._calls[-1] if self._calls else None

    def reset(self) -> None:
        """Forget all recorded calls. The handler is unaffected."""
        self._calls.clear()

    # -- Transport protocol ------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reusable(self) -> bool:
        return not self._closed

    @property
    def in_flight(self) -> int:
        return 0

    @property
    def max_concurrent(self) -> int:
        return 1 << 30  # unlimited for tests

    @property
    def host_port(self) -> str:
        return "mock"

    async def aclose(self) -> None:
        self._closed = True

    async def handle_request(
        self,
        *,
        method: str,
        url: URL,
        headers: Headers,
        body: Any,
        timeout: Optional[float],
    ) -> RawResponse:
        # Build a lightweight Request-shaped object so handlers get the
        # nice ``request.method`` / ``request.url`` / ``request.headers``
        # / ``request.content`` accessors they'd see in real code.
        from hyperhttp.client import Request  # local import — avoid cycle

        request = Request(method=method, url=url, headers=headers, content=body)
        self._calls.append(request)

        result = self._handler(request)
        if inspect.isawaitable(result):
            result = await result
        response = _coerce_response(result)
        return response._build_raw()


# ---------------------------------------------------------------------------
# Router — tiny optional helper for path-dispatching handlers
# ---------------------------------------------------------------------------


class Router:
    """Method + path-prefix dispatcher.

    Tiny on purpose — the point of ``MockTransport`` is that you write your
    own handler. This helper just exists so the 90 % case ("GET /users
    returns 200, everything else 404") is a one-liner:

        router = Router()
        router.get("/users",        lambda req: MockResponse(200, json=[...]))
        router.post("/users",       lambda req: MockResponse(201))
        router.route("GET", "/health", lambda req: MockResponse(204))

        mock = MockTransport(router)

    Matching is **exact** on ``(method, url.path)``. For anything fancier
    (wildcards, path-parameters), dispatch inside your own handler.
    Unmatched requests return ``MockResponse(404)``.
    """

    __slots__ = ("_routes", "_default")

    def __init__(
        self,
        *,
        default: Optional[Union[Handler, MockResponse, int]] = None,
    ) -> None:
        self._routes: dict = {}
        self._default: Handler = (
            _normalise_handler(default)
            if default is not None
            else (lambda _req: MockResponse(404))
        )

    def route(self, method: str, path: str, handler: Handler) -> "Router":
        self._routes[(method.upper(), path)] = _wrap_handler(handler)
        return self

    def get(self, path: str, handler: Handler) -> "Router":
        return self.route("GET", path, handler)

    def post(self, path: str, handler: Handler) -> "Router":
        return self.route("POST", path, handler)

    def put(self, path: str, handler: Handler) -> "Router":
        return self.route("PUT", path, handler)

    def patch(self, path: str, handler: Handler) -> "Router":
        return self.route("PATCH", path, handler)

    def delete(self, path: str, handler: Handler) -> "Router":
        return self.route("DELETE", path, handler)

    def head(self, path: str, handler: Handler) -> "Router":
        return self.route("HEAD", path, handler)

    def options(self, path: str, handler: Handler) -> "Router":
        return self.route("OPTIONS", path, handler)

    def __call__(self, request: Any) -> Any:
        handler = self._routes.get((request.method, request.url.path))
        if handler is None:
            return self._default(request)
        return handler(request)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_response(value: Any) -> MockResponse:
    if isinstance(value, MockResponse):
        return value
    if isinstance(value, int):
        return MockResponse(value)
    raise TypeError(
        f"Handler must return MockResponse or int status, got {type(value).__name__}"
    )


def _wrap_handler(handler: Union[Handler, MockResponse, int]) -> Handler:
    if isinstance(handler, (MockResponse, int)):
        fixed = handler

        def _fixed(_request: Any) -> Any:
            return fixed

        return _fixed
    if callable(handler):
        return handler
    raise TypeError(
        f"Expected a callable, MockResponse, or int status — got {type(handler).__name__}"
    )


def _normalise_handler(
    handler: Union[
        Handler,
        MockResponse,
        int,
        Sequence[Union[MockResponse, int]],
        Mapping[str, Union[MockResponse, int]],
    ],
) -> Handler:
    if isinstance(handler, MockResponse):
        resp = handler

        def _one(_request: Any) -> MockResponse:
            return resp

        return _one

    if isinstance(handler, int):
        status = handler

        def _status(_request: Any) -> MockResponse:
            return MockResponse(status)

        return _status

    # Sequence of responses — pop in order. Loud failure when exhausted.
    if (
        not callable(handler)
        and not isinstance(handler, Mapping)
        and isinstance(handler, (list, tuple))
    ):
        queue: List[Any] = list(handler)

        def _replay(request: Any) -> MockResponse:
            if not queue:
                raise IndexError(
                    "MockTransport replay queue exhausted "
                    f"(request: {request.method} {request.url})"
                )
            return _coerce_response(queue.pop(0))

        return _replay

    if isinstance(handler, Mapping):
        # Mapping keys are "METHOD /path" — simple and human-readable.
        lookup = {
            _parse_route_key(k): _coerce_response(v) for k, v in handler.items()
        }

        def _map(request: Any) -> MockResponse:
            key = (request.method, request.url.path)
            if key not in lookup:
                return MockResponse(404)
            return lookup[key]

        return _map

    if callable(handler):
        return handler

    raise TypeError(
        "MockTransport handler must be a callable, MockResponse, int status, "
        "a sequence of responses, or a {'METHOD /path': MockResponse} mapping"
    )


def _parse_route_key(key: str) -> tuple:
    # "GET /x" -> ("GET", "/x"). "/x" -> ("GET", "/x").
    parts = key.strip().split(None, 1)
    if len(parts) == 1:
        return ("GET", parts[0])
    return (parts[0].upper(), parts[1])
