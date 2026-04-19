"""Unit tests for the zero-copy FastStream protocol."""

import asyncio

import pytest

from hyperhttp.connection._streams import FastStream


pytestmark = pytest.mark.asyncio


class _MockTransport:
    """Minimal transport impl for exercising FastStream without a socket."""

    def __init__(self):
        self.written = bytearray()
        self.closed = False
        self.paused = False

    def write(self, data):
        self.written.extend(data)

    def close(self):
        self.closed = True

    def pause_reading(self):
        self.paused = True

    def resume_reading(self):
        self.paused = False

    def get_extra_info(self, name, default=None):
        return default


async def test_data_received_then_recv_returns_same_object():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.data_received(b"hello")
    chunk = await s.recv()
    # The bytes object handed in should be popped out unchanged — no
    # copy/slice in between.
    assert chunk == b"hello"


async def test_recv_blocks_until_data_or_eof():
    s = FastStream()
    s.connection_made(_MockTransport())
    task = asyncio.create_task(s.recv())
    await asyncio.sleep(0)
    assert not task.done()
    s.data_received(b"x")
    assert await task == b"x"


async def test_eof_returns_empty_bytes():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.eof_received()
    assert await s.recv() == b""


async def test_queued_order_preserved():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.data_received(b"a")
    s.data_received(b"b")
    s.data_received(b"c")
    assert await s.recv() == b"a"
    assert await s.recv() == b"b"
    assert await s.recv() == b"c"


async def test_read_bounded_slices_without_joining_head():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.data_received(b"abcdef")
    head = await s.read(3)
    tail = await s.read(10)
    assert head == b"abc"
    assert tail == b"def"


async def test_read_minus_one_returns_next_chunk_whole():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.data_received(b"abc")
    s.data_received(b"def")
    assert await s.read(-1) == b"abc"
    assert await s.read(-1) == b"def"


async def test_connection_lost_wakes_waiter_with_exception():
    s = FastStream()
    s.connection_made(_MockTransport())
    task = asyncio.create_task(s.recv())
    await asyncio.sleep(0)
    boom = ConnectionResetError("peer reset")
    s.connection_lost(boom)
    with pytest.raises(ConnectionResetError):
        await task


async def test_pause_resume_on_high_low_water():
    from hyperhttp.connection import _streams as mod

    s = FastStream()
    tp = _MockTransport()
    s.connection_made(tp)
    # Push past the high water mark → transport should be paused.
    big = b"x" * (mod._HIGH_WATER + 1)
    s.data_received(big)
    assert tp.paused is True
    # Drain below the low water mark → transport should be resumed.
    await s.recv()
    assert tp.paused is False


async def test_write_forwards_to_transport():
    s = FastStream()
    tp = _MockTransport()
    s.connection_made(tp)
    s.write(b"abc")
    assert bytes(tp.written) == b"abc"


async def test_write_after_close_raises():
    s = FastStream()
    tp = _MockTransport()
    s.connection_made(tp)
    s.close()
    with pytest.raises(ConnectionError):
        s.write(b"x")


async def test_drain_returns_immediately_without_backpressure():
    s = FastStream()
    s.connection_made(_MockTransport())
    await s.drain()  # no-op when no pause_writing was signalled


async def test_drain_waits_for_resume_writing():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.pause_writing()
    task = asyncio.create_task(s.drain())
    await asyncio.sleep(0)
    assert not task.done()
    s.resume_writing()
    await task


async def test_recv_not_reentrant():
    s = FastStream()
    s.connection_made(_MockTransport())
    task = asyncio.create_task(s.recv())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        await s.recv()
    # Unblock the first recv so the test doesn't leak the task.
    s.data_received(b"z")
    await task


async def test_close_idempotent():
    s = FastStream()
    s.connection_made(_MockTransport())
    s.close()
    s.close()  # second call must not raise
