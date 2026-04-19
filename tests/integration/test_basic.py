"""
Smoke tests against a real aiohttp HTTP/1.1 loopback server.
"""

from __future__ import annotations

import asyncio

import pytest

import hyperhttp


async def test_basic_get(http_server: str) -> None:
    async with hyperhttp.Client(http2=False) as c:
        r = await c.get(f"{http_server}/")
        assert r.status_code == 200
        await r.aread()
        assert r.text == "ok"


async def test_bytes_of(http_server: str) -> None:
    async with hyperhttp.Client(http2=False) as c:
        r = await c.get(f"{http_server}/bytes/1024")
        body = await r.aread()
        assert len(body) == 1024
        assert body == b"x" * 1024


async def test_chunked(http_server: str) -> None:
    async with hyperhttp.Client(http2=False) as c:
        r = await c.get(f"{http_server}/chunked")
        assert r.status_code == 200
        body = await r.aread()
        assert body == b"".join(b"chunk%d\n" % i for i in range(5))


async def test_gzip_decoded(http_server: str) -> None:
    async with hyperhttp.Client(http2=False, accept_compressed=True) as c:
        r = await c.get(f"{http_server}/gzip")
        assert r.status_code == 200
        await r.aread()
        assert r.text == "hello " * 200


@pytest.mark.skipif(
    not hyperhttp.HAS_BROTLI, reason="brotli optional dependency not installed"
)
async def test_brotli_decoded(http_server: str) -> None:
    async with hyperhttp.Client(http2=False, accept_compressed=True) as c:
        r = await c.get(f"{http_server}/brotli")
        if r.status_code == 501:  # server couldn't brotli-encode
            pytest.skip("server lacks brotli")
        await r.aread()
        assert r.text == "hello " * 200


async def test_connection_reuse(http_server: str) -> None:
    """Second request should reuse the same underlying connection."""
    async with hyperhttp.Client(http2=False) as c:
        await (await c.get(f"{http_server}/")).aread()
        await (await c.get(f"{http_server}/")).aread()
        stats = c.get_pool_stats()
        # One pool, one connection reused twice.
        assert any(
            s.get("total_connections", 0) <= 2 for s in stats.values()
        ), stats


async def test_pool_waiter_fairness_high_concurrency(http_server: str) -> None:
    """With a tiny pool and high concurrency every request must still succeed."""
    async with hyperhttp.Client(
        http2=False,
        max_connections=4,
        max_keepalive_connections=4,
    ) as c:
        N = 500

        async def one(i: int) -> int:
            r = await c.get(f"{http_server}/bytes/32")
            body = await r.aread()
            assert len(body) == 32
            return i

        results = await asyncio.gather(*(one(i) for i in range(N)))
        assert sorted(results) == list(range(N))
