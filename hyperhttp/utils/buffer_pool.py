"""
Buffer pool for async reads.

Asyncio runs on a single thread per loop, so the pool is intentionally
lock-free. It keeps one free-list per size bucket (powers of two by default)
and returns ``bytearray`` slabs that callers can wrap in ``memoryview``.

For response data that is shared across multiple consumers (for example,
``Response.aiter_bytes`` plus ``Response.aread``), wrap the slab in a
``RefCountedBuffer`` and release it deterministically when the response is
closed. The old weakref/GC-based reclamation is gone — it was unpredictable
and held unnecessary complexity.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

__all__ = ["BufferPool", "RefCountedBuffer", "BufferView"]


# Default size buckets: 4 KiB (headers / small bodies), 16 KiB (typical
# socket read), 64 KiB (large bodies), 256 KiB (1 MiB+ bodies). Callers
# asking for something larger just get a fresh bytearray outside the pool.
_DEFAULT_SIZES: Tuple[int, ...] = (4096, 16384, 65536, 262144)


class BufferPool:
    """A per-loop, lock-free pool of reusable ``bytearray`` slabs."""

    __slots__ = ("_pools", "_sizes", "_max_per_bucket", "_allocated", "_reused")

    def __init__(
        self,
        sizes: Tuple[int, ...] = _DEFAULT_SIZES,
        *,
        max_per_bucket: int = 128,
        initial_count: int = 0,
    ) -> None:
        self._sizes: Tuple[int, ...] = tuple(sorted(sizes))
        self._max_per_bucket = max_per_bucket
        self._pools: Dict[int, Deque[bytearray]] = {size: deque() for size in self._sizes}
        self._allocated = 0
        self._reused = 0
        if initial_count > 0:
            for size in self._sizes:
                for _ in range(initial_count):
                    self._pools[size].append(bytearray(size))

    def _bucket_for(self, minimum_size: int) -> int:
        for size in self._sizes:
            if size >= minimum_size:
                return size
        return minimum_size

    def acquire(self, minimum_size: int) -> Tuple[bytearray, int]:
        """Return a (buffer, size) pair of at least ``minimum_size`` bytes."""
        size = self._bucket_for(minimum_size)
        pool = self._pools.get(size)
        if pool is not None and pool:
            self._reused += 1
            return pool.popleft(), size
        self._allocated += 1
        return bytearray(size), size

    def release(self, buffer: bytearray, size: int) -> None:
        """Return a slab to the pool.

        Slabs not matching a known bucket size are dropped on the floor.
        """
        pool = self._pools.get(size)
        if pool is None:
            return
        if len(pool) >= self._max_per_bucket:
            return
        pool.append(buffer)

    # Alias for callers used to the old API.
    get_buffer = acquire
    return_buffer = release

    def wrap(self, buffer: bytearray, size: int) -> "RefCountedBuffer":
        return RefCountedBuffer(buffer, size, self)

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "allocated": self._allocated,
            "reused": self._reused,
            "buckets": {size: len(pool) for size, pool in self._pools.items()},  # type: ignore[dict-item]
        }


class RefCountedBuffer:
    """Reference-counted owner of a pooled ``bytearray`` slab.

    Use ``view()`` to produce a cheap ``BufferView`` that increments the
    refcount. Call ``release()`` (or close the response) to decrement the
    initial reference. When the count reaches zero the slab is returned to
    the pool.
    """

    __slots__ = ("_buffer", "_size", "_pool", "_refs", "_released")

    def __init__(self, buffer: bytearray, size: int, pool: BufferPool) -> None:
        self._buffer = buffer
        self._size = size
        self._pool = pool
        self._refs = 1
        self._released = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def memoryview(self) -> memoryview:
        if self._released:
            raise RuntimeError("Buffer has been released")
        return memoryview(self._buffer)

    def view(self, start: int = 0, end: Optional[int] = None) -> "BufferView":
        if self._released:
            raise RuntimeError("Buffer has been released")
        if end is None:
            end = self._size
        self._refs += 1
        return BufferView(self, memoryview(self._buffer)[start:end])

    def release(self) -> None:
        self._decref()

    def _decref(self) -> None:
        if self._released:
            return
        self._refs -= 1
        if self._refs <= 0:
            self._released = True
            self._pool.release(self._buffer, self._size)


class BufferView:
    """A ``memoryview`` slice over a ``RefCountedBuffer`` that releases on drop."""

    __slots__ = ("_parent", "_view", "_released")

    def __init__(self, parent: RefCountedBuffer, view: memoryview) -> None:
        self._parent = parent
        self._view = view
        self._released = False

    def __enter__(self) -> "BufferView":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.release()

    def __len__(self) -> int:
        return len(self._view)

    def __bytes__(self) -> bytes:
        return bytes(self._view)

    def tobytes(self) -> bytes:
        return bytes(self._view)

    @property
    def data(self) -> memoryview:
        return self._view

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._parent._decref()
