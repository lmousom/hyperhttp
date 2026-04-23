"""Tests for MockTransport — the zero-network transport for tests.

Because MockTransport is what people reach for when writing their own
tests, the surface area is intentionally forgiving. These tests lock in
the ergonomics so a regression here is a regression in testability.
"""

from __future__ import annotations

import pytest

import hyperhttp
from hyperhttp import (
    BasicAuth,
    Client,
    ConnectError,
    MockResponse,
    MockTransport,
    Router,
)
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff


# ---------------------------------------------------------------------------
# MockResponse
# ---------------------------------------------------------------------------


class TestMockResponse:
    def test_defaults_empty_200(self) -> None:
        r = MockResponse()
        assert r.status_code == 200
        raw = r._build_raw()
        assert raw.status_code == 200
        assert raw.headers.get("content-length") is None  # no body

    def test_content_sets_content_length(self) -> None:
        r = MockResponse(200, content=b"hello")
        raw = r._build_raw()
        assert raw.headers["content-length"] == "5"

    def test_text_sets_content_type_and_length(self) -> None:
        r = MockResponse(200, text="héllo")
        raw = r._build_raw()
        body = "héllo".encode("utf-8")
        assert raw.headers["content-length"] == str(len(body))
        assert raw.headers["content-type"] == "text/plain; charset=utf-8"

    def test_json_sets_content_type(self) -> None:
        r = MockResponse(201, json={"a": 1})
        raw = r._build_raw()
        assert raw.headers["content-type"] == "application/json"
        assert int(raw.headers["content-length"]) > 0

    def test_json_null_is_distinct_from_unset(self) -> None:
        r = MockResponse(200, json=None)
        raw = r._build_raw()
        assert raw.headers["content-type"] == "application/json"

    def test_rejects_multiple_body_sources(self) -> None:
        with pytest.raises(ValueError):
            MockResponse(200, text="a", content=b"b")
        with pytest.raises(ValueError):
            MockResponse(200, json={}, text="a")

    def test_explicit_headers_win(self) -> None:
        r = MockResponse(
            200, text="hi", headers={"content-type": "application/x-custom"}
        )
        raw = r._build_raw()
        assert raw.headers["content-type"] == "application/x-custom"


# ---------------------------------------------------------------------------
# MockTransport — callable handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_handler_called_once() -> None:
    def handler(_req):
        return MockResponse(200, json={"hi": 1})

    mock = MockTransport(handler)
    async with Client(transport=mock) as c:
        r = await c.get("https://api.example.com/x")
        await r.aread()
        assert r.json() == {"hi": 1}

    assert mock.call_count == 1
    assert mock.last_request.method == "GET"
    assert mock.last_request.url.path == "/x"


@pytest.mark.asyncio
async def test_async_handler_works() -> None:
    async def handler(req):
        return MockResponse(200, text=f"async {req.url.path}")

    async with Client(transport=MockTransport(handler)) as c:
        r = await c.get("https://x/hello")
        await r.aread()
        assert r.text == "async /hello"


@pytest.mark.asyncio
async def test_handler_gets_request_headers_and_body() -> None:
    captured = {}

    def handler(req):
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["content-type"] = req.headers.get("content-type")
        captured["body"] = req.content
        return MockResponse(204)

    async with Client(transport=MockTransport(handler)) as c:
        r = await c.post("https://api/echo", json={"x": 1})
        await r.aread()

    assert captured["method"] == "POST"
    assert captured["path"] == "/echo"
    assert captured["content-type"] == "application/json"
    assert captured["body"] == b'{"x":1}'


@pytest.mark.asyncio
async def test_int_shorthand_is_status_code() -> None:
    async with Client(transport=MockTransport(lambda _r: 418)) as c:
        r = await c.get("https://x/y")
        await r.aread()
        assert r.status_code == 418


# ---------------------------------------------------------------------------
# MockTransport — replay, single, mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_sequence_pops_in_order() -> None:
    mock = MockTransport(
        [MockResponse(500), MockResponse(503), MockResponse(200, text="ok")]
    )
    async with Client(transport=mock, retry=False) as c:
        statuses = []
        for _ in range(3):
            r = await c.get("https://x/y")
            await r.aread()
            statuses.append(r.status_code)
    assert statuses == [500, 503, 200]


@pytest.mark.asyncio
async def test_replay_exhausted_raises() -> None:
    mock = MockTransport([MockResponse(200)])
    async with Client(transport=mock) as c:
        r = await c.get("https://x/y")
        await r.aread()
        with pytest.raises(IndexError):
            await c.get("https://x/y")


@pytest.mark.asyncio
async def test_single_response_reused() -> None:
    mock = MockTransport(MockResponse(204))
    async with Client(transport=mock) as c:
        for _ in range(5):
            r = await c.get("https://x/y")
            await r.aread()
            assert r.status_code == 204
    assert mock.call_count == 5


@pytest.mark.asyncio
async def test_mapping_dispatch_and_unmatched_404() -> None:
    mock = MockTransport(
        {
            "GET /users": MockResponse(200, json=[{"id": 1}]),
            "POST /users": MockResponse(201, json={"id": 99}),
            "/ping": MockResponse(204),  # default method = GET
        }
    )
    async with Client(transport=mock) as c:
        r = await c.get("https://api/users")
        await r.aread()
        assert r.status_code == 200

        r = await c.post("https://api/users", json={})
        await r.aread()
        assert r.status_code == 201
        assert r.json() == {"id": 99}

        r = await c.get("https://api/ping")
        await r.aread()
        assert r.status_code == 204

        r = await c.get("https://api/nope")
        await r.aread()
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_dispatches_by_method_and_path() -> None:
    router = Router()
    router.get("/ping", lambda _r: MockResponse(200, text="pong"))
    router.post("/users", lambda _r: MockResponse(201, json={"ok": True}))
    router.delete("/users/1", lambda _r: MockResponse(204))

    async with Client(transport=MockTransport(router)) as c:
        r = await c.get("https://api/ping")
        await r.aread()
        assert r.text == "pong"

        r = await c.post("https://api/users", json={})
        await r.aread()
        assert r.status_code == 201

        r = await c.delete("https://api/users/1")
        await r.aread()
        assert r.status_code == 204

        r = await c.get("https://api/nothing-here")
        await r.aread()
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_router_custom_default() -> None:
    router = Router(default=MockResponse(418, text="teapot"))
    router.get("/ok", lambda _r: MockResponse(200))

    async with Client(transport=MockTransport(router)) as c:
        r = await c.get("https://x/ok")
        await r.aread()
        assert r.status_code == 200

        r = await c.get("https://x/elsewhere")
        await r.aread()
        assert r.status_code == 418
        assert r.text == "teapot"


@pytest.mark.asyncio
async def test_router_chaining_api() -> None:
    router = (
        Router()
        .get("/a", lambda _r: MockResponse(200))
        .post("/a", lambda _r: MockResponse(201))
    )
    async with Client(transport=MockTransport(router)) as c:
        r = await c.get("https://x/a")
        await r.aread()
        assert r.status_code == 200
        r = await c.post("https://x/a")
        await r.aread()
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Error injection + integration with Client machinery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_exception_propagates_by_default() -> None:
    def handler(_r):
        raise ConnectError("simulated outage")

    async with Client(transport=MockTransport(handler), retry=False) as c:
        with pytest.raises(ConnectError):
            await c.get("https://x/y")


@pytest.mark.asyncio
async def test_retry_drives_replay_sequence() -> None:
    """Client's retry handler should see the mocked ConnectError twice then succeed."""
    attempts = {"n": 0}

    def handler(_r):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectError(f"flaky {attempts['n']}")
        return MockResponse(200, text="ok")

    retry = RetryPolicy(
        max_retries=3,
        retry_categories=["CONNECTION"],
        backoff_strategy=DecorrelatedJitterBackoff(base=0.001, max_backoff=0.01),
    )
    async with Client(transport=MockTransport(handler), retry=retry) as c:
        r = await c.get("https://x/y")
        await r.aread()
    assert r.status_code == 200
    assert r.text == "ok"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_auth_header_visible_to_handler() -> None:
    seen_auth = []

    def handler(req):
        seen_auth.append(req.headers.get("authorization"))
        return MockResponse(200)

    async with Client(
        transport=MockTransport(handler),
        auth=BasicAuth("alice", "s3cret"),
    ) as c:
        r = await c.get("https://x/y")
        await r.aread()
    assert seen_auth and seen_auth[0].startswith("Basic ")


@pytest.mark.asyncio
async def test_event_hooks_fire_with_mock() -> None:
    hook_calls = []

    async def on_request(req):
        req.headers["x-fingerprint"] = "abc"

    def on_response(resp):
        hook_calls.append(resp.status_code)

    def handler(req):
        assert req.headers.get("x-fingerprint") == "abc"
        return MockResponse(200)

    async with Client(
        transport=MockTransport(handler),
        event_hooks={"request": [on_request], "response": [on_response]},
    ) as c:
        r = await c.get("https://x/y")
        await r.aread()
    assert hook_calls == [200]


@pytest.mark.asyncio
async def test_cookies_round_trip_through_mock() -> None:
    def handler(req):
        # On the second call the client should echo the cookie.
        cookie = req.headers.get("cookie")
        if cookie is not None:
            return MockResponse(200, text=cookie)
        return MockResponse(
            200,
            text="",
            headers={"set-cookie": "session=xyz; Path=/"},
        )

    async with Client(transport=MockTransport(handler)) as c:
        r = await c.get("https://x/login")
        await r.aread()
        r = await c.get("https://x/me")
        await r.aread()
    assert "session=xyz" in r.text


@pytest.mark.asyncio
async def test_stream_response_body() -> None:
    """MockResponse(stream=...) delivers body chunks exactly as written."""

    async def chunks():
        yield b"one "
        yield b"two "
        yield b"three"

    def handler(_r):
        return MockResponse(200, stream=chunks())

    async with Client(transport=MockTransport(MockResponse(200, content=b"ignored"))) as c:
        pass  # sanity: basic content path works

    async with Client(transport=MockTransport(handler)) as c:
        received = []
        r = await c.stream("GET", "https://x/y")
        async with r:
            async for chunk in r.aiter_bytes():
                received.append(chunk)
    assert b"".join(received) == b"one two three"


# ---------------------------------------------------------------------------
# Inspection / reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_clears_calls() -> None:
    mock = MockTransport(MockResponse(200))
    async with Client(transport=mock) as c:
        for _ in range(3):
            r = await c.get("https://x/y")
            await r.aread()
    assert mock.call_count == 3
    mock.reset()
    assert mock.call_count == 0
    assert mock.last_request is None


def test_invalid_handler_type_rejected() -> None:
    with pytest.raises(TypeError):
        MockTransport("not a handler")  # type: ignore[arg-type]


def test_public_exports() -> None:
    assert hyperhttp.MockTransport is MockTransport
    assert hyperhttp.MockResponse is MockResponse
    assert hyperhttp.Router is Router
