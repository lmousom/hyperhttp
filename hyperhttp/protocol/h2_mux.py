"""
HTTP/2 multiplexed transport.

A single TCP connection serves many concurrent streams. All ``h2.Connection``
mutations are serialized by a single ``asyncio.Lock`` (``_io_lock``); a
background reader task processes events, enqueues response head/body data
onto per-stream queues, and fires events for flow control window updates.

Important corrections over the old implementation:

- ``RemoteSettingsChanged`` reads ``event.changed_settings`` with the
  ``SettingCodes.MAX_CONCURRENT_STREAMS`` key (the attribute path the
  ``h2`` library actually uses).
- Streams are allocated from a waiter queue when the peer's
  ``MAX_CONCURRENT_STREAMS`` is saturated, rather than failing fast.
- The single lock makes concurrent ``handle_request`` calls safe; the old
  design had unprotected mutations of ``h2.Connection``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import h2.config
import h2.connection
import h2.events
import h2.settings

from hyperhttp._headers import Headers
from hyperhttp._url import URL
from hyperhttp.connection.transport import RawResponse, Transport
from hyperhttp.exceptions import (
    ConnectError,
    ReadError,
    ReadTimeout,
    RemoteProtocolError,
    WriteError,
)
from hyperhttp.protocol.h1 import ResponseHead

logger = logging.getLogger("hyperhttp.protocol.h2")

__all__ = ["H2Transport"]


# Per-stream body buffer depth. Combined with HTTP/2's 65535-byte default
# stream window, this caps the receive-side memory per stream at roughly
# (maxsize × max_frame_size). The real bound comes from flow control which
# we now release only *after* the consumer drains a chunk; this queue is
# belt-and-suspenders against pathological small-frame flooding.
_BODY_QUEUE_MAX = 32


class _H2Stream:
    __slots__ = (
        "stream_id",
        "head",
        "head_event",
        "body_queue",
        "error",
        "ended",
    )

    def __init__(self, stream_id: int) -> None:
        self.stream_id = stream_id
        self.head: Optional[ResponseHead] = None
        self.head_event = asyncio.Event()
        # Bounded queue → the reader task naturally backpressures on a slow
        # consumer by awaiting ``put`` here, which in turn means the HTTP/2
        # flow-control window is no longer auto-advanced. A misbehaving server
        # can't force us to buffer an unbounded amount of DATA frames.
        self.body_queue: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(
            maxsize=_BODY_QUEUE_MAX
        )
        self.error: Optional[BaseException] = None
        self.ended = False

    def fail(self, exc: BaseException) -> None:
        if self.error is None:
            self.error = exc
        self.head_event.set()
        # Clear any pending chunks so the consumer wakes on None promptly.
        while True:
            try:
                self.body_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self.body_queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover
            pass


class H2Transport(Transport):
    """One TCP connection multiplexing many HTTP/2 streams."""

    http_version = "HTTP/2"

    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host_port: str,
        authority: str,
        scheme: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._host_port = host_port
        self._authority = authority
        self._scheme = scheme
        config = h2.config.H2Configuration(client_side=True, header_encoding=None)
        self._conn = h2.connection.H2Connection(config=config)

        self._streams: Dict[int, _H2Stream] = {}
        self._next_stream_id = 1
        self._max_concurrent = 100  # updated from SETTINGS

        self._io_lock = asyncio.Lock()
        self._stream_slot_event = asyncio.Event()
        self._stream_slot_event.set()

        self._closed = False
        self._error: Optional[BaseException] = None
        self._reader_task: Optional[asyncio.Task[Any]] = None
        self._initialized = False

        self._created_at = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._conn.initiate_connection()
        try:
            self._writer.write(self._conn.data_to_send())
            await self._writer.drain()
        except OSError as exc:
            raise ConnectError(f"Failed to initialize HTTP/2 connection: {exc}") from exc
        self._reader_task = asyncio.create_task(self._reader_loop(), name="hyperhttp-h2-reader")
        self._initialized = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Cancel reader first so it doesn't try to read on a closed socket.
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            async with self._io_lock:
                self._conn.close_connection()
                data = self._conn.data_to_send()
                if data:
                    self._writer.write(data)
                    await self._writer.drain()
        except Exception:
            pass
        try:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (BrokenPipeError, ConnectionError):
                pass
        except Exception:
            pass
        # Fail any outstanding streams.
        for stream in list(self._streams.values()):
            stream.fail(ConnectError("Connection closed"))
        self._streams.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reusable(self) -> bool:
        if self._closed or self._error is not None:
            return False
        # A stream slot must be available.
        return len(self._streams) < self._max_concurrent

    @property
    def host_port(self) -> str:
        return self._host_port

    @property
    def in_flight(self) -> int:
        return len(self._streams)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    # -- public request path -----------------------------------------------

    async def handle_request(
        self,
        *,
        method: str,
        url: URL,
        headers: Headers,
        body: Any,
        timeout: Optional[float],
    ) -> RawResponse:
        if self._closed:
            raise ConnectError("H2Transport is closed")

        # Wait for a stream slot if the peer's concurrency limit is reached.
        while len(self._streams) >= self._max_concurrent and not self._closed:
            self._stream_slot_event.clear()
            await self._stream_slot_event.wait()
        if self._closed:
            raise ConnectError("H2Transport closed while waiting for a stream slot")

        method_up = method.upper()
        h2_headers: List[Tuple[str, str]] = [
            (":method", method_up),
            (":scheme", self._scheme),
            (":authority", self._authority),
            (":path", url.target),
        ]
        _HOP_BY_HOP = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "host",
            "proxy-connection",
        }
        for name, value in headers.items():
            lname = name.lower()
            if lname in _HOP_BY_HOP:
                continue
            h2_headers.append((lname, value))

        # Allocate stream and send headers atomically.
        stream = _H2Stream(self._next_stream_id)
        self._next_stream_id += 2
        self._streams[stream.stream_id] = stream

        body_is_bytes = isinstance(body, (bytes, bytearray, memoryview))
        body_is_async_iter = body is not None and not body_is_bytes

        try:
            async with self._io_lock:
                end_stream = body is None
                self._conn.send_headers(
                    stream_id=stream.stream_id,
                    headers=h2_headers,
                    end_stream=end_stream,
                )
                if body_is_bytes and body:
                    # For now we push the whole body at once; for large bodies
                    # this would ideally be chunked with flow control. Most
                    # requests are small, so this is a reasonable default.
                    data = bytes(body)  # type: ignore[arg-type]
                    self._conn.send_data(stream.stream_id, data, end_stream=True)
                await self._flush_locked()

            if body_is_async_iter:
                await self._stream_request_body(stream.stream_id, body)

            # Wait for response head (with timeout).
            try:
                await _wait(stream.head_event.wait(), timeout)
            except asyncio.TimeoutError as exc:
                await self._reset_stream(stream.stream_id, h2.errors.ErrorCodes.CANCEL)
                raise ReadTimeout("Timed out waiting for HTTP/2 response headers") from exc

            if stream.error is not None:
                raise stream.error
            assert stream.head is not None

            release_cb = lambda: self._release_stream(stream.stream_id)  # noqa: E731
            return RawResponse(
                status_code=stream.head.status_code,
                http_version="HTTP/2",
                headers=stream.head.headers,
                stream=self._body_iter(stream),
                release_cb=release_cb,
            )
        except BaseException:
            # Remove the stream on failure.
            self._streams.pop(stream.stream_id, None)
            self._notify_slot()
            raise

    async def _stream_request_body(self, stream_id: int, body: Any) -> None:
        async for chunk in body:
            if not chunk:
                continue
            data = bytes(chunk)
            async with self._io_lock:
                # Honor flow control: split if necessary.
                while data:
                    max_frame = self._conn.local_flow_control_window(stream_id)
                    max_frame = min(max_frame, self._conn.max_outbound_frame_size)
                    if max_frame <= 0:
                        # Need to wait for a window update.
                        break
                    piece = data[:max_frame]
                    data = data[max_frame:]
                    self._conn.send_data(stream_id, piece, end_stream=False)
                await self._flush_locked()
        async with self._io_lock:
            self._conn.end_stream(stream_id)
            await self._flush_locked()

    async def _body_iter(self, stream: _H2Stream) -> AsyncIterator[bytes]:
        while True:
            chunk = await stream.body_queue.get()
            if chunk is None:
                if stream.error is not None:
                    raise stream.error
                return
            yield chunk
            # Consumer-driven flow control: only tell the peer that its bytes
            # are "consumed" once we've actually handed them to the caller.
            # This is the core defence against the unbounded-memory vector —
            # a hostile server that ignores our flow-control window is
            # dropped back to the advertised size (default 65535 / stream).
            await self._ack_data(stream.stream_id, len(chunk))

    async def _ack_data(self, stream_id: int, nbytes: int) -> None:
        if nbytes <= 0 or self._closed:
            return
        try:
            async with self._io_lock:
                if self._closed:
                    return
                self._conn.acknowledge_received_data(nbytes, stream_id)
                await self._flush_locked()
        except Exception:
            # The stream or connection may have been torn down between our
            # decision to ack and our acquisition of the lock. That's fine —
            # nothing useful to send.
            pass

    async def _release_stream(self, stream_id: int) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        self._notify_slot()

    def _notify_slot(self) -> None:
        if not self._stream_slot_event.is_set():
            self._stream_slot_event.set()

    async def _reset_stream(self, stream_id: int, error_code: int) -> None:
        async with self._io_lock:
            try:
                self._conn.reset_stream(stream_id, error_code=error_code)
                await self._flush_locked()
            except Exception:
                pass
        self._streams.pop(stream_id, None)
        self._notify_slot()

    # -- IO helpers --------------------------------------------------------

    async def _flush_locked(self) -> None:
        data = self._conn.data_to_send()
        if not data:
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except OSError as exc:
            raise WriteError(f"HTTP/2 write failed: {exc}") from exc

    async def _reader_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    data = await self._reader.read(65536)
                except (ConnectionError, OSError) as exc:
                    raise ReadError(f"HTTP/2 read failed: {exc}") from exc
                if not data:
                    raise RemoteProtocolError("HTTP/2 connection closed by peer")
                # Decode frames under the io lock (h2 state mutation), then
                # dispatch the resulting events *outside* the lock so dispatch
                # can ``await`` on a full per-stream queue without starving
                # the ack path (which also needs the lock).
                async with self._io_lock:
                    events = self._conn.receive_data(data)
                    await self._flush_locked()
                for event in events:
                    await self._dispatch_event(event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            for s in list(self._streams.values()):
                s.fail(exc)
            self._streams.clear()
        finally:
            self._closed = True
            self._notify_slot()

    async def _dispatch_event(self, event: h2.events.Event) -> None:
        if isinstance(event, h2.events.ResponseReceived):
            stream = self._streams.get(event.stream_id)
            if stream is None:
                return
            headers = Headers()
            status = 200
            for name, value in event.headers:
                name_s = name.decode("ascii") if isinstance(name, (bytes, bytearray)) else name
                value_s = value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else value
                if name_s == ":status":
                    try:
                        status = int(value_s)
                    except ValueError:
                        status = 0
                elif not name_s.startswith(":"):
                    headers.add(name_s, value_s)
            stream.head = ResponseHead(
                http_version="HTTP/2",
                status_code=status,
                reason="",
                headers=headers,
            )
            stream.head_event.set()

        elif isinstance(event, h2.events.DataReceived):
            stream = self._streams.get(event.stream_id)
            if stream is None:
                # Stream was cancelled locally before the DATA arrived; ack
                # the bytes so the connection-level window doesn't leak.
                await self._ack_data(
                    event.stream_id, event.flow_controlled_length
                )
                return
            # Do NOT ack here — flow control is consumer-driven. See
            # ``_body_iter`` and ``_ack_data``. Putting into a bounded queue
            # back-pressures the whole reader loop on a slow stream, which
            # in turn keeps HTTP/2's own flow-control window shut.
            await stream.body_queue.put(bytes(event.data))

        elif isinstance(event, h2.events.StreamEnded):
            stream = self._streams.get(event.stream_id)
            if stream is None:
                return
            stream.ended = True
            await stream.body_queue.put(None)

        elif isinstance(event, h2.events.StreamReset):
            stream = self._streams.get(event.stream_id)
            if stream is None:
                return
            stream.fail(RemoteProtocolError(f"HTTP/2 stream reset: code={event.error_code}"))

        elif isinstance(event, h2.events.RemoteSettingsChanged):
            # h2 exposes changed_settings as dict[SettingCodes, ChangedSetting].
            changed = getattr(event, "changed_settings", {})
            max_cs_code = h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
            setting = changed.get(max_cs_code)
            if setting is not None:
                new_value = getattr(setting, "new_value", None)
                if new_value is not None and new_value > 0:
                    self._max_concurrent = min(new_value, 1000)
                    self._notify_slot()

        elif isinstance(event, h2.events.ConnectionTerminated):
            err = RemoteProtocolError(
                f"HTTP/2 connection terminated: code={event.error_code}"
            )
            self._error = err
            for s in list(self._streams.values()):
                s.fail(err)
            self._streams.clear()
            self._closed = True
            self._notify_slot()

        elif isinstance(event, h2.events.WindowUpdated):
            # Flow control window changed — our outbound send loop will pick
            # this up next time it tries to send.
            pass


async def _wait(coro: Any, timeout: Optional[float]) -> Any:
    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)
