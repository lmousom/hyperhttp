"""
DNS cache and Happy Eyeballs v2 (RFC 8305) connector.

``getaddrinfo`` doesn't expose DNS TTLs, so we treat the cache as a
min/max bounded timestamp cache: fresh results live for at least ``min_ttl``
and at most ``max_ttl``. IPv6 and IPv4 results are interleaved so that
Happy Eyeballs races them fairly with the configured stagger (250ms by
default).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from hyperhttp.exceptions import ConnectError, DNSError

logger = logging.getLogger("hyperhttp.utils.dns")

__all__ = ["DNSCache", "DNSResolver", "AddressInfo", "happy_eyeballs_connect"]


class AddressInfo:
    __slots__ = ("family", "sockaddr")

    def __init__(self, family: int, sockaddr: Tuple[Any, ...]):
        self.family = family
        self.sockaddr = sockaddr

    def __repr__(self) -> str:
        return f"AddressInfo(family={self.family}, sockaddr={self.sockaddr})"


class DNSCache:
    """Bounded-TTL cache of ``(host, port) → [AddressInfo...]``."""

    def __init__(self, *, min_ttl: float = 10.0, max_ttl: float = 300.0) -> None:
        self._cache: Dict[Tuple[str, int], Tuple[float, List[AddressInfo]]] = {}
        self._min_ttl = min_ttl
        self._max_ttl = max_ttl
        self._lock = asyncio.Lock()

    async def resolve(self, host: str, port: int) -> List[AddressInfo]:
        now = time.monotonic()
        entry = self._cache.get((host, port))
        if entry and entry[0] > now:
            return entry[1]

        addrs = await self._lookup(host, port)
        async with self._lock:
            self._cache[(host, port)] = (now + self._max_ttl, addrs)
        return addrs

    async def _lookup(self, host: str, port: int) -> List[AddressInfo]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise DNSError(f"DNS resolution failed for {host}: {exc}") from exc
        result = [AddressInfo(family, sockaddr) for family, _, _, _, sockaddr in infos]
        return _interleave_by_family(result)

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    async def invalidate(self, host: str, port: int) -> None:
        async with self._lock:
            self._cache.pop((host, port), None)


def _interleave_by_family(addrs: List[AddressInfo]) -> List[AddressInfo]:
    """Interleave IPv6 and IPv4 results for RFC 8305 ordering."""
    ipv6 = [a for a in addrs if a.family == socket.AF_INET6]
    ipv4 = [a for a in addrs if a.family == socket.AF_INET]
    other = [a for a in addrs if a.family not in (socket.AF_INET, socket.AF_INET6)]
    out: List[AddressInfo] = []
    for i in range(max(len(ipv6), len(ipv4))):
        if i < len(ipv6):
            out.append(ipv6[i])
        if i < len(ipv4):
            out.append(ipv4[i])
    out.extend(other)
    return out


class DNSResolver:
    def __init__(self, cache: Optional[DNSCache] = None) -> None:
        self._cache = cache or DNSCache()

    async def resolve(self, host: str, port: int) -> List[AddressInfo]:
        return await self._cache.resolve(host, port)

    async def invalidate(self, host: str, port: int) -> None:
        await self._cache.invalidate(host, port)


ConnectFactory = Callable[[AddressInfo], Awaitable[Any]]


async def happy_eyeballs_connect(
    addresses: List[AddressInfo],
    connect_factory: ConnectFactory,
    *,
    stagger: float = 0.25,
    timeout: Optional[float] = None,
) -> Any:
    """Race ``connect_factory`` across addresses with RFC 8305 stagger.

    Returns the first successful result. All other pending attempts are
    cancelled. If every attempt fails, the last exception is raised.
    """
    if not addresses:
        raise DNSError("No addresses to connect to")

    loop = asyncio.get_running_loop()
    winner: "asyncio.Future[Any]" = loop.create_future()
    tasks: List[asyncio.Task[Any]] = []
    exceptions: List[BaseException] = []

    async def attempt(addr: AddressInfo, delay: float) -> None:
        if delay:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        if winner.done():
            return
        try:
            conn = await connect_factory(addr)
        except BaseException as exc:  # noqa: BLE001 - we want broad here
            exceptions.append(exc)
            if len(exceptions) == len(addresses) and not winner.done():
                winner.set_exception(
                    _first_real_exception(exceptions)
                    or ConnectError("All connection attempts failed")
                )
            return
        if not winner.done():
            winner.set_result(conn)
        else:
            _close_quietly(conn)

    for idx, addr in enumerate(addresses):
        tasks.append(asyncio.create_task(attempt(addr, stagger * idx)))

    try:
        if timeout is not None:
            return await asyncio.wait_for(asyncio.shield(winner), timeout)
        return await winner
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Await cancellation so we don't leak tasks.
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _first_real_exception(excs: List[BaseException]) -> Optional[BaseException]:
    for exc in excs:
        if not isinstance(exc, asyncio.CancelledError):
            return exc
    return None


def _close_quietly(obj: Any) -> None:
    try:
        close = getattr(obj, "close", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)
    except Exception:
        pass
