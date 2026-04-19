"""
Connection management for HyperHTTP.

Public surface:

- ``Transport`` (abstract): one TCP/TLS pipe; speaks H1 or H2.
- ``H1Transport`` / ``H2Transport``: protocol-specific transports.
- ``ConnectionPool`` / ``ConnectionPoolManager``: per-host and global pools.
- ``connect_transport``: open a transport with ALPN-aware dispatch.
"""

from hyperhttp.connection.pool import (
    ConnectionPool,
    ConnectionPoolManager,
    PoolOptions,
)
from hyperhttp.connection.transport import (
    H1Transport,
    RawResponse,
    Transport,
    connect_transport,
)

__all__ = [
    "Transport",
    "H1Transport",
    "RawResponse",
    "ConnectionPool",
    "ConnectionPoolManager",
    "PoolOptions",
    "connect_transport",
]
