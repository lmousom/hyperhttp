"""End-to-end tests for Client event hooks."""

from __future__ import annotations

import socket
from typing import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web as aioweb

import hyperhttp
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def echo_server() -> AsyncIterator[str]:
    """Server that echoes back the request's Authorization + X-Trace headers."""

    async def echo(request: aioweb.Request) -> aioweb.Response:
        return aioweb.json_response(
            {
                "authorization": request.headers.get("Authorization"),
                "trace": request.headers.get("X-Trace"),
                "method": request.method,
                "path": request.path,
            }
        )

    # A flaky endpoint: closes the TCP connection before any bytes flow on
    # the first two attempts (a ``RemoteProtocolError`` on the client —
    # classified as CONNECTION, which the retry policy treats as retryable).
    attempts = {"count": 0}

    async def flaky(request: aioweb.Request) -> aioweb.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            # Slam the transport shut without responding.
            transport = request.transport
            if transport is not None:
                transport.close()
            return aioweb.Response()  # Unreachable on the wire.
        return aioweb.json_response({"ok": True, "attempt": attempts["count"]})

    app = aioweb.Application()
    app.router.add_get("/echo", echo)
    app.router.add_get("/flaky", flaky)
    app.router.add_post("/echo", echo)
    runner = aioweb.AppRunner(app, access_log=None)
    await runner.setup()
    port = _pick_port()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_request_hook_mutation_hits_server(echo_server: str) -> None:
    def inject_trace(request) -> None:
        request.headers["X-Trace"] = "trace-id-42"

    async with hyperhttp.Client(
        event_hooks={"request": [inject_trace]},
        trust_env=False,
        retry=False,
    ) as client:
        r = await client.get(f"{echo_server}/echo")
        await r.aread()
        assert r.status_code == 200
        body = r.json()
        assert body["trace"] == "trace-id-42"


@pytest.mark.asyncio
async def test_response_hook_fires_with_response(echo_server: str) -> None:
    seen = []

    async def record(response) -> None:
        seen.append(response.status_code)

    async with hyperhttp.Client(
        event_hooks={"response": [record]},
        trust_env=False,
        retry=False,
    ) as client:
        r = await client.get(f"{echo_server}/echo")
        await r.aread()
        assert seen == [200]


@pytest.mark.asyncio
async def test_sync_and_async_hooks_coexist(echo_server: str) -> None:
    order = []

    def sync_hook(request) -> None:
        order.append("sync")

    async def async_hook(request) -> None:
        order.append("async")

    async with hyperhttp.Client(
        event_hooks={"request": [sync_hook, async_hook]},
        trust_env=False,
        retry=False,
    ) as client:
        r = await client.get(f"{echo_server}/echo")
        await r.aread()

    assert order == ["sync", "async"]


@pytest.mark.asyncio
async def test_hooks_fire_per_retry_attempt(echo_server: str) -> None:
    """Flaky endpoint: 2× 500 then 200. Hooks should fire 3 times each."""
    request_count = 0
    response_statuses: list = []

    def on_request(_request) -> None:
        nonlocal request_count
        request_count += 1

    def on_response(response) -> None:
        response_statuses.append(response.status_code)

    retry = RetryPolicy(
        max_retries=3,
        retry_categories=["CONNECTION", "TRANSIENT", "TIMEOUT", "PROTOCOL"],
        backoff_strategy=DecorrelatedJitterBackoff(base=0.001, max_backoff=0.01),
    )
    async with hyperhttp.Client(
        event_hooks={"request": [on_request], "response": [on_response]},
        retry=retry,
        trust_env=False,
    ) as client:
        r = await client.get(f"{echo_server}/flaky")
        await r.aread()

    assert r.status_code == 200
    # Request hook fires per attempt: 2 failed (connection closed) + 1 ok.
    assert request_count == 3
    # Response hook only fires on attempts that actually produced a response.
    assert response_statuses == [200]


@pytest.mark.asyncio
async def test_hook_exception_propagates(echo_server: str) -> None:
    class BoomError(RuntimeError):
        pass

    def boom(_request) -> None:
        raise BoomError("nope")

    async with hyperhttp.Client(
        event_hooks={"request": [boom]},
        trust_env=False,
        retry=False,
    ) as client:
        with pytest.raises(BoomError):
            await client.get(f"{echo_server}/echo")


@pytest.mark.asyncio
async def test_hooks_can_be_appended_at_runtime(echo_server: str) -> None:
    seen = []

    async with hyperhttp.Client(trust_env=False, retry=False) as client:
        client.event_hooks["response"].append(
            lambda r: seen.append(r.status_code)
        )
        r = await client.get(f"{echo_server}/echo")
        await r.aread()

    assert seen == [200]


@pytest.mark.asyncio
async def test_request_hook_interacts_with_auth(echo_server: str) -> None:
    """Request hook should see the final headers including BasicAuth."""
    seen = []

    def record(request) -> None:
        seen.append(request.headers.get("authorization"))

    async with hyperhttp.Client(
        auth=("alice", "s3cret"),
        event_hooks={"request": [record]},
        trust_env=False,
        retry=False,
    ) as client:
        r = await client.get(f"{echo_server}/echo")
        await r.aread()
        assert r.json()["authorization"] == seen[0]
        assert seen[0].startswith("Basic ")


@pytest.mark.asyncio
async def test_unknown_hook_rejected() -> None:
    with pytest.raises(ValueError):
        hyperhttp.Client(event_hooks={"whoops": []})
