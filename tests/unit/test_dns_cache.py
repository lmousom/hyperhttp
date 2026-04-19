import asyncio
import socket

import pytest

from hyperhttp.exceptions import ConnectError, DNSError
from hyperhttp.utils.dns_cache import (
    AddressInfo,
    DNSCache,
    DNSResolver,
    _interleave_by_family,
    happy_eyeballs_connect,
)


pytestmark = pytest.mark.asyncio


async def test_interleave_by_family():
    v6a = AddressInfo(socket.AF_INET6, ("::1", 80, 0, 0))
    v6b = AddressInfo(socket.AF_INET6, ("::2", 80, 0, 0))
    v4 = AddressInfo(socket.AF_INET, ("127.0.0.1", 80))
    out = _interleave_by_family([v4, v6a, v6b])
    assert [a.sockaddr[0] for a in out] == ["::1", "127.0.0.1", "::2"]


async def test_resolver_uses_cache(monkeypatch):
    cache = DNSCache(min_ttl=1.0, max_ttl=60.0)
    call_count = {"n": 0}

    async def fake_lookup(host, port):
        call_count["n"] += 1
        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    out1 = await cache.resolve("example.com", 80)
    out2 = await cache.resolve("example.com", 80)
    assert out1 == out2
    assert call_count["n"] == 1


async def test_resolver_refreshes_after_expiry(monkeypatch):
    cache = DNSCache(min_ttl=0.0, max_ttl=0.0)
    call_count = {"n": 0}

    async def fake_lookup(host, port):
        call_count["n"] += 1
        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    await cache.resolve("example.com", 80)
    # Force the cached deadline to the past.
    cache._cache[("example.com", 80)] = (0.0, cache._cache[("example.com", 80)][1])
    await cache.resolve("example.com", 80)
    assert call_count["n"] == 2


async def test_resolver_invalidate(monkeypatch):
    cache = DNSCache()

    async def fake_lookup(host, port):
        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    resolver = DNSResolver(cache)
    await resolver.resolve("example.com", 80)
    assert ("example.com", 80) in cache._cache
    await resolver.invalidate("example.com", 80)
    assert ("example.com", 80) not in cache._cache


async def test_cache_clear(monkeypatch):
    cache = DNSCache()

    async def fake_lookup(host, port):
        return [AddressInfo(socket.AF_INET, ("127.0.0.1", port))]

    monkeypatch.setattr(cache, "_lookup", fake_lookup)

    await cache.resolve("a.example", 80)
    await cache.resolve("b.example", 80)
    await cache.clear()
    assert cache._cache == {}


async def test_lookup_wraps_gaierror(monkeypatch):
    cache = DNSCache()

    loop = asyncio.get_running_loop()

    async def boom(*args, **kwargs):
        raise socket.gaierror(-2, "nodename")

    monkeypatch.setattr(loop, "getaddrinfo", boom)

    with pytest.raises(DNSError):
        await cache._lookup("no.such.host", 80)


async def test_happy_eyeballs_empty_raises():
    with pytest.raises(DNSError):
        await happy_eyeballs_connect([], lambda a: asyncio.sleep(0))


async def test_happy_eyeballs_first_wins():
    addrs = [
        AddressInfo(socket.AF_INET6, ("::1", 80, 0, 0)),
        AddressInfo(socket.AF_INET, ("127.0.0.1", 80)),
    ]

    async def factory(addr):
        # The first address should win because it starts immediately.
        await asyncio.sleep(0)
        return addr.sockaddr[0]

    result = await happy_eyeballs_connect(addrs, factory, stagger=0.01)
    assert result == "::1"


async def test_happy_eyeballs_falls_through_on_error():
    addrs = [
        AddressInfo(socket.AF_INET6, ("::1", 80, 0, 0)),
        AddressInfo(socket.AF_INET, ("127.0.0.1", 80)),
    ]

    async def factory(addr):
        if addr.family == socket.AF_INET6:
            raise ConnectError("boom")
        await asyncio.sleep(0)
        return "ok"

    result = await happy_eyeballs_connect(addrs, factory, stagger=0.01)
    assert result == "ok"


async def test_happy_eyeballs_all_fail_raises_last():
    addrs = [
        AddressInfo(socket.AF_INET6, ("::1", 80, 0, 0)),
        AddressInfo(socket.AF_INET, ("127.0.0.1", 80)),
    ]

    async def factory(addr):
        raise ConnectError(f"fail {addr.family}")

    with pytest.raises(ConnectError):
        await happy_eyeballs_connect(addrs, factory, stagger=0.01)


async def test_happy_eyeballs_timeout():
    addrs = [AddressInfo(socket.AF_INET, ("127.0.0.1", 80))]

    async def slow(addr):
        await asyncio.sleep(5)
        return "never"

    with pytest.raises(asyncio.TimeoutError):
        await happy_eyeballs_connect(addrs, slow, stagger=0.01, timeout=0.05)
