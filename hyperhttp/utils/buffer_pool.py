"""
Memory management utilities for efficient buffer reuse.
"""

import collections
import time
import weakref
from typing import Dict, Tuple, List, Deque, Optional, Set, Any, Union

class RefCountedBuffer:
    """
    A reference-counted buffer that tracks usage and returns to pool when done.
    
    This enables safe buffer reuse while preventing premature recycling.
    """
    
    def __init__(self, buffer: bytearray, size: int, pool: "BufferPool"):
        self._buffer = buffer
        self._size = size
        self._pool = pool
        self._refcount = 1
        self._view = memoryview(buffer)
        
    def get_view(self, start: int = 0, end: Optional[int] = None) -> "BufferView":
        """Create a slice view without copying the data."""
        if end is None:
            end = self._size
        return BufferView(self, self._view[start:end])
        
    def increment_ref(self) -> None:
        """Increment the reference count."""
        self._refcount += 1
        
    def decrement_ref(self) -> None:
        """Decrement the reference count and return to pool if zero."""
        self._refcount -= 1
        if self._refcount == 0:
            # Return buffer to pool when no more references
            self._pool.return_buffer(self._buffer, self._size)
            

class BufferView:
    """
    A view into a reference-counted buffer.
    
    This provides a way to work with a portion of a buffer without copying,
    while ensuring the underlying buffer isn't recycled prematurely.
    """
    
    def __init__(self, parent_buffer: RefCountedBuffer, view: memoryview):
        self._parent = parent_buffer
        self._view = view
        self._parent.increment_ref()
        
    def __del__(self) -> None:
        """Automatically decrement reference when view is garbage collected."""
        self._parent.decrement_ref()
        
    @property
    def data(self) -> memoryview:
        """Get the underlying view data."""
        return self._view
    
    def tobytes(self) -> bytes:
        """Convert view to bytes (copies data)."""
        return self._view.tobytes()


class BufferPool:
    """
    Pool of reusable memory buffers to minimize allocations.
    
    This class maintains separate pools of differently sized buffers for
    efficient memory reuse in HTTP request/response handling.
    """
    
    def __init__(self, sizes: Tuple[int, ...] = (4096, 16384, 65536), initial_count: int = 8):
        """
        Initialize the buffer pool.
        
        Args:
            sizes: Tuple of buffer sizes to pre-allocate
            initial_count: Number of each size to pre-allocate
        """
        # Maintain pools of different-sized buffers
        self._pools: Dict[int, Deque[bytearray]] = {
            size: collections.deque() for size in sizes
        }
        self._sizes = sorted(sizes)
        
        # Track statistics for pool behavior optimization
        self._stats = {
            "gets": 0,
            "returns": 0,
            "misses": 0,
            "oversize_allocs": 0,
            "last_cleanup": time.time(),
        }
        
        # Set of weak references to all RefCountedBuffers
        # This helps detect leaks during cleanup
        self._active_buffers: Set[weakref.ref] = set()
        
        # Pre-allocate buffers to reduce initial allocation pressure
        for size in sizes:
            for _ in range(initial_count):
                self._pools[size].append(bytearray(size))
                
    def get_buffer(self, minimum_size: int) -> Tuple[bytearray, int]:
        """
        Get a buffer of at least the specified size.
        
        Args:
            minimum_size: Minimum required buffer size
            
        Returns:
            Tuple of (buffer, size)
        """
        self._stats["gets"] += 1
        
        # Find the smallest buffer size that satisfies the request
        for size in self._sizes:
            if size >= minimum_size:
                # Reuse existing buffer if available
                if self._pools[size]:
                    buf = self._pools[size].popleft()
                    return buf, size
                
                # No existing buffer, allocate a new one of this size
                self._stats["misses"] += 1
                buf = bytearray(size)
                return buf, size
                
        # If no predefined size is large enough, create custom-sized buffer
        # (rare case handling for very large payloads)
        self._stats["oversize_allocs"] += 1
        return bytearray(minimum_size), minimum_size
    
    def get_ref_counted_buffer(self, minimum_size: int) -> RefCountedBuffer:
        """
        Get a reference-counted buffer wrapper.
        
        This is the preferred method for getting buffers that will be
        shared between components or returned in response objects.
        
        Args:
            minimum_size: Minimum required buffer size
            
        Returns:
            RefCountedBuffer object
        """
        buffer, size = self.get_buffer(minimum_size)
        
        # Create reference-counted wrapper
        ref_buffer = RefCountedBuffer(buffer, size, self)
        
        # Track with weak reference for leak detection
        self._active_buffers.add(weakref.ref(ref_buffer))
        
        return ref_buffer
        
    def return_buffer(self, buffer: bytearray, size: int) -> None:
        """
        Return a buffer to the pool for reuse.
        
        Args:
            buffer: Buffer to return
            size: Size category of the buffer
        """
        self._stats["returns"] += 1
        
        # Only return to pool if it matches a predefined size
        if size in self._pools:
            # Clear buffer contents to prevent memory leaks
            # Use zero for safety, though this has a performance cost
            buffer[:] = b"\x00" * len(buffer)
            self._pools[size].append(buffer)
            
        # Periodically clean up oversized pools
        current_time = time.time()
        if current_time - self._stats["last_cleanup"] > 60:  # Every minute
            self._cleanup_pools()
            self._stats["last_cleanup"] = current_time
            
    def _cleanup_pools(self) -> None:
        """
        Periodically clean up pools to prevent excessive memory usage.
        
        This removes excess buffers from each pool if they're not being used.
        """
        # Clean up weak references to deleted buffers
        self._active_buffers = set(ref for ref in self._active_buffers if ref() is not None)
        
        # Calculate target size based on usage patterns
        for size, pool in self._pools.items():
            # Keep at most 2x the number of buffers currently in use
            active_count = sum(1 for ref in self._active_buffers 
                              if ref() is not None and ref()._size == size)
            
            # Always keep some minimum number of buffers
            target_size = max(8, active_count * 2)
            
            # Remove excess buffers
            while len(pool) > target_size:
                pool.pop()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get buffer pool statistics."""
        stats = dict(self._stats)
        
        # Add current pool sizes
        stats["pools"] = {size: len(pool) for size, pool in self._pools.items()}
        
        # Add active buffer count
        stats["active_buffers"] = len(self._active_buffers)
        
        return stats