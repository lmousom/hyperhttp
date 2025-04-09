"""
Memory management utilities for efficient buffer reuse.
"""

import collections
import time
import weakref
from threading import Lock
from typing import Dict, Tuple, Deque, Optional, Set, Any

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
        self._last_access = time.monotonic()
        self._access_count = 0
        
    def get_view(self, start: int = 0, end: Optional[int] = None) -> "BufferView":
        """Create a slice view without copying the data."""
        self._access_count += 1
        self._last_access = time.monotonic()
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
        
        # Add thread safety locks
        self._locks = {size: Lock() for size in sizes}
        self._global_lock = Lock()
        
        # Add size-specific active buffer tracking
        self._active_counts = {size: 0 for size in sizes}
        
        # Track statistics for pool behavior optimization
        self._stats = {
            "gets": 0,
            "returns": 0,
            "misses": 0,
            "oversize_allocs": 0,
            "last_cleanup": time.time(),
            "allocation_patterns": collections.defaultdict(int),
            "fragmentation_ratio": 0.0
        }
        
        # Memory management thresholds
        self._allocation_threshold = 0.8  # 80% utilization triggers pre-allocation
        self._fragmentation_threshold = 0.3  # 30% fragmentation triggers defragmentation
        
        # Set of weak references to all RefCountedBuffers
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
        # Update stats without holding the global lock for long
        with self._global_lock:
            self._stats["gets"] += 1
            self._stats["allocation_patterns"][minimum_size] += 1
        
        # Find appropriate size first, then lock only that pool
        target_size = next((size for size in self._sizes if size >= minimum_size), minimum_size)
        
        if target_size in self._pools:
            with self._locks[target_size]:
                if self._pools[target_size]:
                    buf = self._pools[target_size].popleft()
                    self._check_allocation_threshold(target_size)
                    return buf, target_size
                
            # Only update stats after failed attempt
            with self._global_lock:
                self._stats["misses"] += 1
        else:
            # If no predefined size is large enough
            with self._global_lock:
                self._stats["oversize_allocs"] += 1
                
        return bytearray(target_size), target_size
    
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
        
        # Track active buffer count by size
        if size in self._active_counts:
            with self._global_lock:
                self._active_counts[size] += 1
        
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
        with self._global_lock:
            self._stats["returns"] += 1
            if size in self._active_counts:
                self._active_counts[size] = max(0, self._active_counts[size] - 1)
        
        # Only return to pool if it matches a predefined size
        if size in self._pools:
            with self._locks[size]:
                # Zero only the first few bytes as a security measure
                # Full zeroing isn't always necessary
                buffer[:64] = b"\x00" * min(64, len(buffer))
                self._pools[size].append(buffer)
            
        # Periodically clean up oversized pools and check fragmentation
        current_time = time.time()
        if current_time - self._stats["last_cleanup"] > 60:  # Every minute
            self._cleanup_pools()
            self._stats["last_cleanup"] = current_time
            
    def _check_allocation_threshold(self, size: int) -> None:
        """Check if we need to pre-allocate more buffers based on utilization."""
        pool = self._pools[size]
        active_count = self._active_counts.get(size, 0)
        total_capacity = len(pool) + active_count
        
        # Use exponential growth for sudden spikes
        if active_count / total_capacity > self._allocation_threshold:
            current_time = time.monotonic()
            
            # Initialize or get last growth time
            if not hasattr(self, '_last_growth_times'):
                self._last_growth_times = {}
            
            last_growth_time = self._last_growth_times.get(size, 0)
            time_since_last = current_time - last_growth_time
            
            # Calculate growth factor based on demand
            if time_since_last < 1.0:
                # Rapid growth needed - use exponential scaling
                growth_factor = min(1.0, 0.25 * (1.0 / time_since_last))
            else:
                # Normal growth rate
                growth_factor = 0.25
                
            # Calculate growth size with minimum guarantee
            growth_size = max(4, int(total_capacity * growth_factor))
            
            # Pre-allocate buffers
            with self._locks[size]:
                for _ in range(growth_size):
                    pool.append(bytearray(size))
            
            # Update last growth time
            self._last_growth_times[size] = current_time
            
            # Update stats
            with self._global_lock:
                if "pre_allocations" not in self._stats:
                    self._stats["pre_allocations"] = 0
                self._stats["pre_allocations"] += growth_size

    def _calculate_fragmentation(self) -> float:
        """Calculate memory fragmentation ratio with caching"""
        current_time = time.monotonic()
        
        # Initialize or check cache timeout
        if not hasattr(self, '_last_fragmentation_check'):
            self._last_fragmentation_check = current_time
            self._last_fragmentation = self._compute_full_fragmentation()
            return self._last_fragmentation
            
        # Only recompute every 5 seconds
        if current_time - self._last_fragmentation_check > 5:
            self._last_fragmentation = self._compute_full_fragmentation()
            self._last_fragmentation_check = current_time
            
        return self._last_fragmentation
        
    def _compute_full_fragmentation(self) -> float:
        """Compute actual fragmentation ratio"""
        with self._global_lock:
            total_allocated = sum(size * len(pool) for size, pool in self._pools.items())
            # Use active counts instead of iterating through weak references
            total_used = sum(size * count for size, count in self._active_counts.items())
            
            self._stats["fragmentation_ratio"] = (
                (total_allocated - total_used) / total_allocated if total_allocated > 0 else 0
            )
            return self._stats["fragmentation_ratio"]

    def _defragment_pool(self, size: int, pool: Deque[bytearray]) -> None:
        """Consolidate partially used buffers for a specific size"""
        with self._locks[size]:
            active_count = sum(1 for ref in self._active_buffers 
                             if ref() is not None and ref()._size == size)
            
            # Keep enough buffers to handle current load plus some headroom
            target_size = max(8, int(active_count * 1.5))
            while len(pool) > target_size:
                pool.pop()

    def _cleanup_pools(self) -> None:
        """
        Periodically clean up pools to prevent excessive memory usage.
        
        This removes excess buffers from each pool if they're not being used
        and handles defragmentation if needed.
        """
        with self._global_lock:
            # Clean up weak references to deleted buffers
            self._active_buffers = set(ref for ref in self._active_buffers if ref() is not None)
            
            # Check fragmentation
            if self._calculate_fragmentation() > self._fragmentation_threshold:
                # Defragment all pools
                for size, pool in self._pools.items():
                    self._defragment_pool(size, pool)
            
            # Analyze allocation patterns and adjust pool sizes
            for size, pool in self._pools.items():
                active_count = self._active_counts.get(size, 0)
                
                # Keep enough buffers to handle current load plus adaptive headroom
                current_time = time.monotonic()
                if hasattr(self, '_last_growth_times') and size in self._last_growth_times:
                    time_since_growth = current_time - self._last_growth_times[size]
                    # If recent growth, keep more headroom
                    if time_since_growth < 60:  # Within last minute
                        headroom_factor = 2.0
                    else:
                        headroom_factor = 1.5
                else:
                    headroom_factor = 1.5
                    
                target_size = max(8, int(active_count * headroom_factor))
                
                # Remove excess buffers gradually
                with self._locks[size]:
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