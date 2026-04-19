"""
DNSCache semantics: hit, miss, TTL expiry, invalidation.
"""

from __future__ import annotations

import asyncio
import time

from hyperhttp.utils.dns_cache import AddressInfo, DNSCache


async def test_dns_cache_hits_within_ttl(monkeypatch) -> None:
    cache = DNSCache(min_ttl=0.05, max_ttl=0.2)

    calls = 0

    async def fake_lookup(host: str, port: int):
        nonlocal calls
        calls += 1
        import socket

        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    await cache.resolve("example.com", 80)
    await cache.resolve("example.com", 80)
    await cache.resolve("example.com", 80)
    assert calls == 1


async def test_dns_cache_refreshes_after_ttl(monkeypatch) -> None:
    cache = DNSCache(min_ttl=0.01, max_ttl=0.02)

    calls = 0

    async def fake_lookup(host: str, port: int):
        nonlocal calls
        calls += 1
        import socket

        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    await cache.resolve("example.com", 80)
    assert calls == 1
    await asyncio.sleep(0.03)
    await cache.resolve("example.com", 80)
    assert calls == 2


async def test_dns_cache_invalidate(monkeypatch) -> None:
    cache = DNSCache(min_ttl=10.0, max_ttl=60.0)

    calls = 0

    async def fake_lookup(host: str, port: int):
        nonlocal calls
        calls += 1
        import socket

        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    await cache.resolve("example.com", 80)
    await cache.invalidate("example.com", 80)
    await cache.resolve("example.com", 80)
    assert calls == 2
