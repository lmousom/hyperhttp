"""
Utility modules for HyperHTTP.

This package contains various utilities used throughout the library,
including memory management, DNS caching, and backoff strategies.
"""

from hyperhttp.utils.buffer_pool import BufferPool, RefCountedBuffer, BufferView
from hyperhttp.utils.backoff import (
    BackoffStrategy,
    ExponentialBackoff,
    DecorrelatedJitterBackoff,
    AdaptiveBackoff,
)
from hyperhttp.utils.dns_cache import DNSCache, DNSResolver

__all__ = [
    "BufferPool",
    "RefCountedBuffer",
    "BufferView",
    "BackoffStrategy",
    "ExponentialBackoff",
    "DecorrelatedJitterBackoff",
    "AdaptiveBackoff",
    "DNSCache",
    "DNSResolver",
]