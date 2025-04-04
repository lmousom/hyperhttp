"""
Connection management for HyperHTTP.

This package handles low-level connection creation, pooling, and lifecycle
management to maximize connection reuse and minimize latency.
"""

from hyperhttp.connection.base import Connection, ConnectionMetadata
from hyperhttp.connection.pool import ConnectionPool, ConnectionPoolManager
from hyperhttp.connection.manager import ConnectionManager

__all__ = [
    "Connection",
    "ConnectionMetadata",
    "ConnectionPool",
    "ConnectionPoolManager",
    "ConnectionManager",
]