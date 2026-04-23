"""
A minimal asyncio Protocol that replaces ``asyncio.StreamReader`` on the
HTTP/1.1 hot path.

Why: ``StreamReader`` buffers incoming data into an internal ``bytearray``
(one copy) and then hands each ``read()`` back as a fresh ``bytes(slice)``
(a second copy). For a 1 MiB body that's ~2 MiB of unnecessary memmove
on top of the kernel→userspace copy and the caller's final accumulation
buffer, which is why our memory footprint on large downloads was ~4x
aiohttp's.

``FastStream`` keeps incoming data as a deque of the original ``bytes``
objects the event loop hands us. ``recv()`` pops them directly — zero
copies beyond the kernel recv. Flow control is preserved via the
standard ``pause_reading()`` / ``resume_reading()`` signals.

It also exposes just enough of the ``StreamReader``/``StreamWriter`` surface
(``write``, ``drain``, ``close``, ``wait_closed``, ``get_extra_info``) for
``H1Transport`` to use it as a drop-in replacement.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from collections import deque
from typing import Any, Deque, Optional, Tuple

from hyperhttp.exceptions import ConnectError, ConnectTimeout, TLSError

__all__ = ["FastStream", "open_fast_stream", "upgrade_to_tls"]


# High/low watermarks for pause_reading. 2 MiB is large enough that a
# typical 1 MiB response lands without ever tripping the pause path, but
# small enough that under backpressure we don't sit on huge amounts of
# unread data per connection (which balloons RSS under concurrency).
_HIGH_WATER = 2 * 1024 * 1024
_LOW_WATER = _HIGH_WATER // 4


class FastStream(asyncio.Protocol):
    """Zero-copy byte stream built directly on ``asyncio.Protocol``.

    The Protocol *is* the reader and writer — we don't wrap it. Bytes
    received from the transport are queued as-is; ``recv()`` returns them
    unchanged.
    """

    __slots__ = (
        "_loop",
        "_queue",
        "_buffered",
        "_read_waiter",
        "_write_waiter",
        "_eof",
        "_exception",
        "_transport",
        "_paused",
        "_closed",
    )

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._queue: Deque[bytes] = deque()
        self._buffered = 0
        self._read_waiter: Optional[asyncio.Future] = None
        self._write_waiter: Optional[asyncio.Future] = None
        self._eof = False
        self._exception: Optional[BaseException] = None
        self._transport: Optional[asyncio.Transport] = None
        self._paused = False
        self._closed = False

    # ------------------------------------------------------------------
    # asyncio.Protocol callbacks
    # ------------------------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        self._closed = True
        if exc is not None:
            self._exception = exc
        self._eof = True
        self._wake_reader()
        self._wake_writer(exc)

    def data_received(self, data: bytes) -> None:
        if not data:
            return
        self._queue.append(data)
        self._buffered += len(data)
        # Apply backpressure only when genuinely backed up, so a 1 MiB body
        # (which comes in 16-64 KiB recvs) flows through without pause/resume
        # churn.
        if (
            self._buffered > _HIGH_WATER
            and not self._paused
            and self._transport is not None
        ):
            try:
                self._transport.pause_reading()
                self._paused = True
            except (AttributeError, NotImplementedError):
                pass
        self._wake_reader()

    def eof_received(self) -> bool:
        self._eof = True
        self._wake_reader()
        return False  # let asyncio close the write side too

    def pause_writing(self) -> None:
        if self._write_waiter is None or self._write_waiter.done():
            self._write_waiter = self._loop.create_future()

    def resume_writing(self) -> None:
        self._wake_writer(None)

    # ------------------------------------------------------------------
    # Reader API
    # ------------------------------------------------------------------

    async def recv(self) -> bytes:
        """Return the next ``bytes`` chunk, or ``b""`` on EOF.

        The returned bytes object is the exact one handed to us by the
        event loop — no intermediate buffers, no copies.
        """
        while not self._queue:
            if self._exception is not None:
                raise self._exception
            if self._eof:
                return b""
            await self._wait_for_data()
        chunk = self._queue.popleft()
        self._buffered -= len(chunk)
        self._maybe_resume()
        return chunk

    async def read(self, n: int = -1) -> bytes:
        """``asyncio.StreamReader``-compatible read.

        Prefer ``recv()`` on the HTTP/1.1 body path — ``read(n)`` may need to
        slice/rejoin chunks to honor the ``n`` bound, which costs one copy.
        Used by the HTTP/2 transport where framing sits above the byte
        stream and the small ``n`` is dictated by the h2 library.
        """
        if n == 0:
            return b""
        while not self._queue:
            if self._exception is not None:
                raise self._exception
            if self._eof:
                return b""
            await self._wait_for_data()
        chunk = self._queue.popleft()
        clen = len(chunk)
        if n < 0 or n >= clen:
            self._buffered -= clen
            self._maybe_resume()
            return chunk
        # ``n`` is smaller than the head of the queue — slice it and push
        # the tail back for the next read.
        head = chunk[:n]
        tail = chunk[n:]
        self._queue.appendleft(tail)
        self._buffered -= n
        self._maybe_resume()
        return head

    def _maybe_resume(self) -> None:
        if (
            self._paused
            and self._buffered < _LOW_WATER
            and self._transport is not None
        ):
            try:
                self._transport.resume_reading()
                self._paused = False
            except (AttributeError, NotImplementedError):
                pass

    async def _wait_for_data(self) -> None:
        if self._read_waiter is not None and not self._read_waiter.done():
            raise RuntimeError("FastStream.recv is not reentrant")
        self._read_waiter = self._loop.create_future()
        try:
            await self._read_waiter
        finally:
            self._read_waiter = None

    def _wake_reader(self) -> None:
        w = self._read_waiter
        if w is not None and not w.done():
            w.set_result(None)

    def _wake_writer(self, exc: Optional[BaseException]) -> None:
        w = self._write_waiter
        if w is not None and not w.done():
            if exc is None:
                w.set_result(None)
            else:
                w.set_exception(exc)
        self._write_waiter = None

    # ------------------------------------------------------------------
    # Writer API (minimal StreamWriter-shaped surface)
    # ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._transport is None or self._closed:
            raise ConnectionError("FastStream is not connected")
        self._transport.write(data)

    async def drain(self) -> None:
        if self._exception is not None:
            raise self._exception
        if self._write_waiter is None:
            return
        try:
            await self._write_waiter
        finally:
            self._write_waiter = None

    def close(self) -> None:
        if self._transport is not None and not self._closed:
            self._closed = True
            try:
                self._transport.close()
            except Exception:
                pass

    async def wait_closed(self) -> None:
        # asyncio transports don't expose an explicit close-completion
        # future; the connection_lost callback will flip ``_closed`` for us.
        if self._closed and self._transport is None:
            return
        # Give the loop one cycle to finish ``connection_lost`` bookkeeping.
        await asyncio.sleep(0)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if self._transport is None:
            return default
        return self._transport.get_extra_info(name, default)

    @property
    def transport(self) -> Optional[asyncio.BaseTransport]:
        return self._transport

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def at_eof(self) -> bool:
        return self._eof and not self._queue


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


async def open_fast_stream(
    *,
    host: str,
    port: int,
    ssl_context: Optional[ssl.SSLContext],
    server_hostname: Optional[str],
    timeout: Optional[float],
    rcvbuf: Optional[int] = 1 << 21,  # 2 MiB SO_RCVBUF — fewer recv() syscalls
    sndbuf: Optional[int] = 1 << 20,  # 1 MiB SO_SNDBUF
) -> FastStream:
    """Open a TCP (+ optional TLS) connection attached to a ``FastStream``.

    Returns the ready-to-use ``FastStream`` after the handshake completes.
    The returned object quacks like both a reader and a writer, so callers
    can use it in place of ``(reader, writer)`` pairs from
    ``asyncio.open_connection``.
    """
    loop = asyncio.get_running_loop()
    stream = FastStream(loop)

    def factory() -> FastStream:
        return stream

    try:
        coro = loop.create_connection(
            factory,
            host=host,
            port=port,
            ssl=ssl_context,
            server_hostname=server_hostname if ssl_context else None,
            ssl_handshake_timeout=timeout if ssl_context else None,
        )
        if timeout is not None:
            await asyncio.wait_for(coro, timeout=timeout)
        else:
            await coro
    except asyncio.TimeoutError as exc:
        raise ConnectTimeout(f"Connection to {host}:{port} timed out") from exc
    except ssl.SSLError as exc:
        raise TLSError(f"TLS handshake with {host}:{port} failed: {exc}") from exc
    except OSError as exc:
        raise ConnectError(f"Connection to {host}:{port} failed: {exc}") from exc

    # Tune the underlying socket for large-body throughput.
    sock: Optional[socket.socket] = stream.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if rcvbuf is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
                except OSError:
                    pass
            if sndbuf is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
                except OSError:
                    pass
        except OSError:
            pass

    return stream


async def upgrade_to_tls(
    stream: "FastStream",
    *,
    ssl_context: ssl.SSLContext,
    server_hostname: str,
    timeout: Optional[float],
) -> "FastStream":
    """Upgrade an already-connected plaintext ``FastStream`` to TLS in-place.

    Used for ``CONNECT``-style HTTPS-through-proxy tunnelling: after we've
    read the proxy's ``200`` response we hand the raw TCP transport to
    ``loop.start_tls`` and swap in the new SSL transport.

    The returned object is the same ``FastStream`` instance with its inner
    transport replaced by the SSL one.
    """
    loop = asyncio.get_running_loop()
    old_transport = stream._transport
    if old_transport is None or stream._closed:
        raise ConnectError("Cannot upgrade a closed stream to TLS")
    if stream._queue:
        # Any bytes already queued on the plaintext side would be lost by
        # ``start_tls`` — the CONNECT response consumer must drain them first.
        raise ConnectError(
            "Unexpected bytes buffered before TLS upgrade; proxy may be misbehaving"
        )

    try:
        coro = loop.start_tls(
            old_transport,
            stream,
            ssl_context,
            server_hostname=server_hostname,
            ssl_handshake_timeout=timeout,
        )
        new_transport = await (
            asyncio.wait_for(coro, timeout=timeout) if timeout is not None else coro
        )
    except asyncio.TimeoutError as exc:
        raise ConnectTimeout(
            f"TLS handshake with {server_hostname} timed out"
        ) from exc
    except ssl.SSLError as exc:
        raise TLSError(
            f"TLS handshake with {server_hostname} failed: {exc}"
        ) from exc
    except OSError as exc:
        raise ConnectError(
            f"TLS handshake with {server_hostname} failed: {exc}"
        ) from exc
    if new_transport is None:
        raise TLSError(f"TLS handshake with {server_hostname} returned no transport")
    stream._transport = new_transport  # type: ignore[assignment]
    return stream
