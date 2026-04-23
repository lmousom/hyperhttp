"""
Regression tests for the connection-pool waiter cancellation race.

Scenario: an ``acquire()`` call waits for a free transport, hits its timeout,
but *between* the timeout firing and the exception reaching our handler, a
concurrent ``release()`` has already handed a transport off to the cancelled
waiter. The old implementation left that transport stranded in ``_active``
forever, slowly exhausting the global connection budget.
"""

from __future__ import annotations

import asyncio

import pytest

from hyperhttp._proxy import ProxyConfig
from hyperhttp._url import URL
from hyperhttp.connection.pool import ConnectionPool, PoolOptions
from hyperhttp.utils.buffer_pool import BufferPool


class _FakeTransport:
    """Minimal stand-in for a real transport. H1-style: one-shot per request."""

    http_version = "HTTP/1.1"
    closed = False
    reusable = True

    async def aclose(self) -> None:
        self.closed = True


class _FakeManager:
    def __init__(self, max_total: int) -> None:
        self.sem = asyncio.Semaphore(max_total)
        self.total_released = 0

    async def _reserve_global(self) -> None:
        await self.sem.acquire()

    def _release_global(self) -> None:
        self.sem.release()
        self.total_released += 1

    @property
    def total_connections(self) -> int:
        return 0


async def _make_pool(max_per_host: int = 1, max_total: int = 1) -> ConnectionPool:
    opts = PoolOptions(
        max_connections=max_total,
        max_connections_per_host=max_per_host,
        keepalive_expiry=60.0,
        http2=False,
        verify=False,
        cert=None,
        ssl_context=None,
        connect_timeout=5.0,
        happy_eyeballs_delay=0.25,
        proxy_config=ProxyConfig(None, trust_env=False),
    )
    pool = ConnectionPool(
        "example.com",
        443,
        "https",
        options=opts,
        dns=None,  # type: ignore[arg-type]
        buffer_pool=BufferPool(),
        manager=_FakeManager(max_total),  # type: ignore[arg-type]
    )
    return pool


async def test_handoff_won_by_release_after_timeout_releases_transport() -> None:
    """A transport handed off to a just-timed-out waiter must not leak.

    We drive the exact race state directly: a waiter future that has already
    received a transport via ``set_result`` (handoff) but whose caller has
    already observed a timeout and is on the error path. The pre-fix code
    cancelled the waiter unconditionally and lost the transport; the fixed
    code detects the winning handoff and returns the transport to the pool.
    """
    pool = await _make_pool(max_per_host=1, max_total=1)
    t = _FakeTransport()

    # Hand-off simulation: the transport has been placed in _active by
    # ``_hand_off_to_waiter`` and ``set_result`` was called on the waiter.
    loop = asyncio.get_running_loop()
    waiter: "asyncio.Future[_FakeTransport]" = loop.create_future()
    pool._waiters.append(waiter)
    pool._active[id(t)] = t
    waiter.set_result(t)

    # Now the caller's timeout branch runs. Pre-fix this would strand ``t``.
    pool._reclaim_waiter_or_cancel(waiter)

    assert id(t) not in pool._active, (
        "transport leaked in _active after timeout-handoff race"
    )
    # H1 transports return to the idle deque so the next caller can pick them.
    assert t in list(pool._idle)


async def test_reclaim_on_plain_cancel_does_not_put_back_live_transport() -> None:
    """If the waiter really was cancelled (no handoff), nothing to reclaim."""
    pool = await _make_pool(max_per_host=1, max_total=1)
    loop = asyncio.get_running_loop()
    waiter: "asyncio.Future[_FakeTransport]" = loop.create_future()
    pool._waiters.append(waiter)

    pool._reclaim_waiter_or_cancel(waiter)

    assert waiter.cancelled()
    assert not pool._idle
    assert not pool._active


async def test_normal_timeout_without_handoff_still_raises_pool_timeout() -> None:
    """Plain timeout (no handoff) still propagates as a PoolTimeout."""
    from hyperhttp.exceptions import PoolTimeout

    pool = await _make_pool(max_per_host=1, max_total=1)

    # Fill the pool.
    t = _FakeTransport()
    pool._active[id(t)] = t
    await pool._manager._reserve_global()

    with pytest.raises(PoolTimeout):
        await pool.acquire(URL("https://example.com/"), timeout=0.02)

    # No waiter left dangling.
    assert all(w.done() or w.cancelled() for w in pool._waiters)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
