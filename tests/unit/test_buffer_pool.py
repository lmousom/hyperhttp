import pytest

from hyperhttp.utils.buffer_pool import BufferPool, BufferView, RefCountedBuffer


def test_acquire_returns_bucket_size():
    pool = BufferPool(sizes=(4096, 16384))
    buf, size = pool.acquire(100)
    assert size == 4096
    assert len(buf) == 4096


def test_acquire_oversize_allocates_exact():
    pool = BufferPool(sizes=(1024,))
    buf, size = pool.acquire(5000)
    assert size == 5000
    assert len(buf) == 5000


def test_release_then_acquire_reuses_slab():
    pool = BufferPool(sizes=(1024,))
    a, size = pool.acquire(100)
    pool.release(a, size)
    b, size2 = pool.acquire(100)
    assert b is a
    assert size2 == size


def test_release_drops_unknown_size():
    pool = BufferPool(sizes=(1024,))
    # 9999 isn't a tracked bucket → silently dropped.
    pool.release(bytearray(9999), 9999)


def test_release_respects_max_per_bucket():
    pool = BufferPool(sizes=(256,), max_per_bucket=1)
    a, size = pool.acquire(100)
    b, _ = pool.acquire(100)
    pool.release(a, size)
    pool.release(b, size)  # second release should be discarded
    assert len(pool._pools[256]) == 1


def test_initial_count_prewarms_pools():
    pool = BufferPool(sizes=(128,), initial_count=3)
    assert len(pool._pools[128]) == 3


def test_stats_counts_allocated_vs_reused():
    pool = BufferPool(sizes=(64,))
    a, size = pool.acquire(32)
    pool.release(a, size)
    pool.acquire(32)
    stats = pool.stats
    assert stats["allocated"] == 1
    assert stats["reused"] == 1


def test_aliases_are_available():
    # ``get_buffer``/``return_buffer`` are alias method names for
    # ``acquire``/``release`` to support older call sites.
    assert BufferPool.get_buffer is BufferPool.acquire
    assert BufferPool.return_buffer is BufferPool.release


def test_refcounted_buffer_release_returns_to_pool():
    pool = BufferPool(sizes=(64,))
    buf, size = pool.acquire(32)
    rc = pool.wrap(buf, size)
    rc.release()
    # After release the slab should be back in the bucket.
    assert any(b is buf for b in pool._pools[64])


def test_refcounted_view_increments_and_releases():
    pool = BufferPool(sizes=(64,))
    buf, size = pool.acquire(32)
    rc = pool.wrap(buf, size)
    with rc.view(0, 8) as v:
        assert isinstance(v, BufferView)
        assert len(v) == 8
        assert bytes(v) == bytes(v.data) == bytes(buf[:8])
    # Primary ref still held.
    assert not rc._released
    rc.release()
    assert rc._released


def test_refcounted_use_after_release_raises():
    pool = BufferPool(sizes=(64,))
    buf, size = pool.acquire(32)
    rc = pool.wrap(buf, size)
    rc.release()
    with pytest.raises(RuntimeError):
        _ = rc.memoryview
    with pytest.raises(RuntimeError):
        rc.view()


def test_refcounted_double_release_noop():
    pool = BufferPool(sizes=(64,))
    buf, size = pool.acquire(32)
    rc = pool.wrap(buf, size)
    rc.release()
    rc.release()  # must not blow up


def test_bufferview_double_release_noop():
    pool = BufferPool(sizes=(64,))
    buf, size = pool.acquire(32)
    rc = pool.wrap(buf, size)
    v = rc.view()
    v.release()
    v.release()
