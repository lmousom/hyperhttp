"""
Connection pool.

Design goals:

- Per-host pool + global cap enforced by ``ConnectionPoolManager``.
- HTTP/2 transports are shared; an HTTP/2 connection is only "busy" when its
  peer-advertised ``MAX_CONCURRENT_STREAMS`` limit is saturated.
- HTTP/1.1 transports are dedicated — at most one in-flight request per
  transport. They go on an idle deque when released and are reused LIFO.
- Waiter fairness: waiters live in a deque of futures and are fulfilled in
  FIFO order. Release hands off directly to the first un-cancelled waiter.
- Global cap ties into request queueing: if we'd exceed the global cap we
  wait for *any* pool to free a slot.

The pool is purely async and relies on the event loop's cooperative
scheduling for correctness — it does not use threading locks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional

from hyperhttp._url import URL
from hyperhttp.connection.transport import Transport, connect_transport
from hyperhttp.exceptions import PoolClosed, PoolTimeout
from hyperhttp.utils.buffer_pool import BufferPool
from hyperhttp.utils.dns_cache import DNSResolver

logger = logging.getLogger("hyperhttp.pool")

__all__ = ["ConnectionPool", "ConnectionPoolManager", "PoolOptions"]


class PoolOptions:
    """Configuration bundle for pool + transport creation."""

    __slots__ = (
        "max_connections",
        "max_connections_per_host",
        "keepalive_expiry",
        "http2",
        "verify",
        "cert",
        "ssl_context",
        "connect_timeout",
        "happy_eyeballs_delay",
    )

    def __init__(
        self,
        *,
        max_connections: int = 100,
        max_connections_per_host: int = 20,
        keepalive_expiry: float = 120.0,
        http2: bool = True,
        verify: Any = True,
        cert: Any = None,
        ssl_context: Any = None,
        connect_timeout: Optional[float] = 10.0,
        happy_eyeballs_delay: float = 0.25,
    ) -> None:
        self.max_connections = max_connections
        self.max_connections_per_host = max_connections_per_host
        self.keepalive_expiry = keepalive_expiry
        self.http2 = http2
        self.verify = verify
        self.cert = cert
        self.ssl_context = ssl_context
        self.connect_timeout = connect_timeout
        self.happy_eyeballs_delay = happy_eyeballs_delay


class ConnectionPool:
    """Per-host pool of transports."""

    def __init__(
        self,
        host: str,
        port: int,
        scheme: str,
        *,
        options: PoolOptions,
        dns: DNSResolver,
        buffer_pool: BufferPool,
        manager: "ConnectionPoolManager",
    ) -> None:
        self._host = host
        self._port = port
        self._scheme = scheme
        self._options = options
        self._dns = dns
        self._buffer_pool = buffer_pool
        self._manager = manager

        self._idle: Deque[Transport] = deque()
        self._active: Dict[int, Transport] = {}
        self._waiters: Deque["asyncio.Future[Transport]"] = deque()
        self._pending_connects = 0
        self._closed = False
        # Serialize the *first* connect for HTTPS pools so concurrent waiters
        # can share the eventual H2 multiplex rather than racing into N
        # separate TLS handshakes only to discover ALPN picked H2.
        self._probing_alpn = scheme == "https" and options.http2
        self._probe_done: Optional["asyncio.Event"] = None

    # -- public ------------------------------------------------------------

    @property
    def host_port(self) -> str:
        default = 443 if self._scheme == "https" else 80
        if self._port == default:
            return self._host
        return f"{self._host}:{self._port}"

    @property
    def total(self) -> int:
        return len(self._active) + len(self._idle) + self._pending_connects

    async def acquire(self, url: URL, *, timeout: Optional[float] = None) -> Transport:
        if self._closed:
            raise PoolClosed(f"Pool for {self.host_port} is closed")

        # Fast path: reusable idle transport.
        transport = self._take_idle()
        if transport is not None:
            self._active[id(transport)] = transport
            return transport

        # HTTP/2 multiplexing: scan active transports for available streams.
        for transport in self._active.values():
            if (
                transport.http_version == "HTTP/2"
                and transport.reusable
                and transport.in_flight < transport.max_concurrent
            ):
                return transport

        # HTTPS + h2 enabled: the first connect for this host races ALPN. If
        # the server picks H2, a single connection serves everyone; if it picks
        # H1, we'll fall through to the normal per-request path. So block
        # sibling waiters until the probe resolves.
        if self._probing_alpn and self._probe_done is not None:
            try:
                await asyncio.wait_for(self._probe_done.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise PoolTimeout(
                    f"Timed out waiting for ALPN probe to {self.host_port}"
                ) from exc
            # Retry the acquire logic post-probe.
            return await self.acquire(url, timeout=timeout)

        # Create a new connection if we have headroom (per-host and global).
        if self._can_create_new():
            probe_event: Optional[asyncio.Event] = None
            if self._probing_alpn and self._probe_done is None:
                probe_event = asyncio.Event()
                self._probe_done = probe_event

            await self._manager._reserve_global()
            self._pending_connects += 1
            try:
                transport = await self._create_transport(url)
            except BaseException:
                self._pending_connects -= 1
                self._manager._release_global()
                if probe_event is not None:
                    self._probe_done = None
                    probe_event.set()
                raise
            self._pending_connects -= 1
            self._active[id(transport)] = transport
            if probe_event is not None:
                # If ALPN chose HTTP/1.1, further callers should not block —
                # disable the probe gate permanently for this host.
                if transport.http_version != "HTTP/2":
                    self._probing_alpn = False
                self._probe_done = None
                probe_event.set()
            return transport

        # Otherwise queue up and wait.
        waiter: "asyncio.Future[Transport]" = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            if timeout is not None:
                return await asyncio.wait_for(asyncio.shield(waiter), timeout)
            return await waiter
        except asyncio.TimeoutError as exc:
            waiter.cancel()
            self._prune_cancelled_waiters()
            raise PoolTimeout(
                f"Timed out waiting for a connection to {self.host_port}"
            ) from exc
        except BaseException:
            waiter.cancel()
            self._prune_cancelled_waiters()
            raise

    def release(self, transport: Transport, *, discard: bool = False) -> None:
        """Return a transport to the pool.

        For HTTP/1.1 the transport is placed back on the idle deque (unless
        ``discard`` is set or it's no longer reusable). For HTTP/2 the
        transport remains active if it still has capacity.
        """
        tid = id(transport)
        if transport.http_version == "HTTP/2":
            if discard or transport.closed or not transport.reusable:
                self._active.pop(tid, None)
                asyncio.create_task(self._close_transport(transport))
                self._manager._release_global()
            if self._waiters:
                self._wake_waiters_h2()
            return

        # HTTP/1.1: transport is done with its single request.
        self._active.pop(tid, None)
        if discard or transport.closed or not transport.reusable:
            asyncio.create_task(self._close_transport(transport))
            self._manager._release_global()
            if self._waiters:
                self._wake_waiters_h2()  # try H2 active for waiters anyway
            return

        if self._waiters and self._hand_off_to_waiter(transport):
            return
        self._idle.append(transport)

    async def aclose(self) -> None:
        self._closed = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(PoolClosed(f"Pool for {self.host_port} is closed"))
        self._waiters.clear()
        for transport in list(self._idle):
            await self._close_transport(transport)
            self._manager._release_global()
        self._idle.clear()
        for transport in list(self._active.values()):
            await self._close_transport(transport)
            self._manager._release_global()
        self._active.clear()

    # -- helpers -----------------------------------------------------------

    def _take_idle(self) -> Optional[Transport]:
        now = time.monotonic()
        expiry = self._options.keepalive_expiry
        while self._idle:
            transport = self._idle.pop()  # LIFO for locality
            if transport.closed or not transport.reusable:
                asyncio.create_task(self._close_transport(transport))
                self._manager._release_global()
                continue
            last_used = getattr(transport, "_last_used", now)
            if expiry and (now - last_used) > expiry:
                asyncio.create_task(self._close_transport(transport))
                self._manager._release_global()
                continue
            return transport
        return None

    def _can_create_new(self) -> bool:
        if self.total >= self._options.max_connections_per_host:
            return False
        if self._manager.total_connections >= self._options.max_connections:
            return False
        return True

    async def _create_transport(self, url: URL) -> Transport:
        return await connect_transport(
            url,
            dns=self._dns,
            ssl_context=self._options.ssl_context,
            verify=self._options.verify,
            cert=self._options.cert,
            connect_timeout=self._options.connect_timeout,
            happy_eyeballs_delay=self._options.happy_eyeballs_delay,
            buffer_pool=self._buffer_pool,
            enable_http2=self._options.http2,
        )

    def _hand_off_to_waiter(self, transport: Transport) -> bool:
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.cancelled() or waiter.done():
                continue
            self._active[id(transport)] = transport
            waiter.set_result(transport)
            return True
        return False

    def _wake_waiters_h2(self) -> None:
        """For HTTP/2 fan-out: hand any waiter to an active conn with capacity."""
        if not self._waiters:
            return
        for transport in self._active.values():
            if transport.http_version != "HTTP/2":
                continue
            if transport.reusable and transport.in_flight < transport.max_concurrent:
                while self._waiters:
                    waiter = self._waiters.popleft()
                    if waiter.cancelled() or waiter.done():
                        continue
                    waiter.set_result(transport)
                    return

    def _prune_cancelled_waiters(self) -> None:
        self._waiters = deque(w for w in self._waiters if not (w.cancelled() or w.done()))

    async def _close_transport(self, transport: Transport) -> None:
        try:
            await transport.aclose()
        except Exception:
            pass


class ConnectionPoolManager:
    """Multi-host connection pool manager with a global connection cap."""

    def __init__(
        self,
        *,
        options: Optional[PoolOptions] = None,
        dns: Optional[DNSResolver] = None,
        buffer_pool: Optional[BufferPool] = None,
    ) -> None:
        self._options = options or PoolOptions()
        self._dns = dns or DNSResolver()
        self._buffer_pool = buffer_pool or BufferPool()
        self._pools: Dict[str, ConnectionPool] = {}
        self._total_slots = 0
        self._global_waiters: Deque["asyncio.Future[None]"] = deque()
        self._closed = False

    # -- global slot accounting -------------------------------------------

    @property
    def total_connections(self) -> int:
        return self._total_slots

    async def _reserve_global(self) -> None:
        while self._total_slots >= self._options.max_connections and not self._closed:
            fut: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
            self._global_waiters.append(fut)
            try:
                await fut
            except BaseException:
                if not fut.done():
                    fut.cancel()
                raise
        if self._closed:
            raise PoolClosed("Pool manager is closed")
        self._total_slots += 1

    def _release_global(self) -> None:
        if self._total_slots > 0:
            self._total_slots -= 1
        while self._global_waiters:
            waiter = self._global_waiters.popleft()
            if not waiter.done() and not waiter.cancelled():
                waiter.set_result(None)
                break

    # -- public API --------------------------------------------------------

    async def acquire(self, url: URL, *, timeout: Optional[float] = None) -> Transport:
        if self._closed:
            raise PoolClosed("Pool manager is closed")
        key = f"{url.scheme}://{url.host_port}"
        pool = self._pools.get(key)
        if pool is None:
            pool = ConnectionPool(
                host=url.host,
                port=url.port,
                scheme=url.scheme,
                options=self._options,
                dns=self._dns,
                buffer_pool=self._buffer_pool,
                manager=self,
            )
            self._pools[key] = pool
        return await pool.acquire(url, timeout=timeout)

    def release(self, transport: Transport, *, discard: bool = False) -> None:
        # Find the pool that owns this transport.
        for pool in self._pools.values():
            if id(transport) in pool._active or transport in pool._idle:  # type: ignore[operator]
                pool.release(transport, discard=discard)
                return
        # Transport wasn't tracked — close defensively.
        asyncio.create_task(transport.aclose())

    async def aclose(self) -> None:
        self._closed = True
        pools = list(self._pools.values())
        self._pools.clear()
        for pool in pools:
            await pool.aclose()
        for waiter in self._global_waiters:
            if not waiter.done():
                waiter.set_exception(PoolClosed("Pool manager is closed"))
        self._global_waiters.clear()

    def pools(self) -> Iterable[ConnectionPool]:
        return self._pools.values()

    def stats(self) -> Dict[str, Dict[str, int]]:
        """Return a per-host snapshot of connection counts."""
        return {
            pool.host_port: {
                "idle": len(pool._idle),
                "active": len(pool._active),
                "pending": pool._pending_connects,
                "waiters": len(pool._waiters),
                "total_connections": pool.total,
            }
            for pool in self._pools.values()
        }
