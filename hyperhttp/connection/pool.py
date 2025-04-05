"""
Connection pooling system for efficient connection reuse.
"""

import asyncio
import collections
import logging
import time
import urllib.parse
from typing import Dict, Any, Deque, Optional, Set, List, Tuple, TypeVar, Type, Callable

from hyperhttp.connection.base import Connection, ConnectionMetadata
from hyperhttp.protocol.http1 import HTTP1Connection
from hyperhttp.protocol.http2 import HTTP2Connection

# Type for connection factories
ConnectionFactory = Callable[..., Connection]

# Logger
logger = logging.getLogger("hyperhttp.connection.pool")


class PoolTimeoutError(Exception):
    """Exception raised when a connection cannot be acquired within the timeout."""
    pass


class ConnectionPool:
    """
    Pool of connections to a specific host.
    
    This manages connections to a single host (hostname:port combination),
    handling connection creation, reuse, and lifecycle.
    """
    
    def __init__(
        self,
        hostname: str,
        port: int,
        scheme: str,
        min_connections: int = 0,
        max_connections: int = 20,
        max_keepalive: float = 120,
        ttl_check_interval: float = 15,
    ):
        self._hostname = hostname
        self._port = port
        self._scheme = scheme
        self._use_tls = scheme == "https"
        
        # Pool configuration
        self._min_connections = min_connections
        self._max_connections = max_connections
        self.max_idle_time = max_keepalive
        self.health_check_interval = ttl_check_interval
        
        # Connection tracking
        self._idle_connections: Deque[Connection] = collections.deque()
        self._active_connections: Set[Connection] = set()
        self._pending_connections = 0
        
        # For connection requests when pool is exhausted
        self._waiting_queue: asyncio.Queue = asyncio.Queue()
        
        # Connection factory based on protocol
        self._factory = self._create_connection_factory()
        
        # Host key for pool lookup
        self.host_key = f"{hostname}:{port}"
        
        # Validation tracking
        self._last_validation_time = time.monotonic()
        self._validation_success_count = 0
        self._validation_failure_count = 0
        
    def _create_connection_factory(self) -> ConnectionFactory:
        """Create a connection factory for this pool."""
        if self._scheme == "https":
            return lambda: HTTP2Connection(
                host=self._hostname,
                port=self._port,
                use_tls=True,
            )
        else:
            return lambda: HTTP1Connection(
                host=self._hostname,
                port=self._port,
                use_tls=False,
            )
    
    async def acquire(self, timeout: Optional[float] = 10.0) -> Connection:
        """
        Acquire a connection from the pool.
        
        Args:
            timeout: Maximum time to wait for a connection
            
        Returns:
            Connection object
            
        Raises:
            PoolTimeoutError: If no connection is available within the timeout
        """
        # Fast path: get idle connection if available
        while self._idle_connections:
            conn = self._idle_connections.popleft()
            
            # Validate connection is still usable
            if await self._validate_connection(conn):
                self._active_connections.add(conn)
                return conn
                
            # Connection was stale, discard and try next
            await self._close_connection(conn)
        
        # Medium path: create new connection if under limit
        total_connections = (len(self._active_connections) + 
                           len(self._idle_connections) + 
                           self._pending_connections)
                           
        if total_connections < self._max_connections:
            self._pending_connections += 1
            try:
                conn = await self._create_connection()
                self._active_connections.add(conn)
                return conn
            finally:
                self._pending_connections -= 1
        
        # Slow path: wait for a connection to be released
        future = asyncio.Future()
        await self._waiting_queue.put(future)
        
        # Set timeout
        if timeout is not None:
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                # Remove from queue if still waiting
                future.cancel()
                raise PoolTimeoutError(
                    f"Timeout waiting for connection to {self._hostname}:{self._port}"
                )
        return await future
    
    def release(self, connection: Connection, recycle: bool = True) -> None:
        """
        Release a connection back to the pool.
        
        Args:
            connection: The connection to release
            recycle: Whether to recycle the connection or close it
        """
        # Remove from active set
        self._active_connections.discard(connection)
        
        if not recycle or not connection.is_reusable():
            # Close non-reusable connections
            asyncio.create_task(self._close_connection(connection))
            return
            
        # Update connection metadata
        connection.metadata.idle_since = time.monotonic()
        
        # Check if anyone is waiting for a connection
        if not self._waiting_queue.empty():
            future = asyncio.create_task(self._waiting_queue.get())
            future.add_done_callback(
                lambda f: self._fulfill_waiting_request(f.result(), connection)
            )
        else:
            # Return to idle pool
            self._idle_connections.append(connection)
    
    def _fulfill_waiting_request(self, future: asyncio.Future, connection: Connection) -> None:
        """Fulfill a waiting connection request."""
        if not future.cancelled():
            self._active_connections.add(connection)
            future.set_result(connection)
        else:
            # Request was cancelled, return connection to pool
            self._idle_connections.append(connection)
    
    async def _create_connection(self) -> Connection:
        """Create a new connection to the host."""
        conn = self._factory()
        try:
            await conn.connect()
            return conn
        except Exception as e:
            logger.warning(f"Failed to create connection to {self.host_key}: {e}")
            await conn.close()
            raise
    
    async def _validate_connection(self, connection: Connection) -> bool:
        """
        Validate that a connection is still usable.
        
        Args:
            connection: Connection to validate
            
        Returns:
            True if the connection is valid, False otherwise
        """
        # Check if connection is marked as non-reusable
        if connection.metadata.marked_for_close:
            return False
            
        # Adaptive validation based on success rate
        current_time = time.monotonic()
        time_since_last_validation = current_time - self._last_validation_time
        
        # Calculate validation success rate
        total_validations = self._validation_success_count + self._validation_failure_count
        if total_validations > 0:
            success_rate = self._validation_success_count / total_validations
        else:
            success_rate = 1.0
            
        # Skip validation if:
        # 1. Connection was recently used (within 5 seconds)
        # 2. High success rate (>95%) and not too much time passed
        if (current_time - connection.metadata.last_used < 5 or
            (success_rate > 0.95 and time_since_last_validation < 30)):
            return True
            
        # Update last validation time
        self._last_validation_time = current_time
        
        try:
            # Perform actual validation
            is_healthy = await connection.check_health()
            
            # Update validation stats
            if is_healthy:
                self._validation_success_count += 1
            else:
                self._validation_failure_count += 1
                
            return is_healthy
        except Exception:
            self._validation_failure_count += 1
            return False
    
    async def _close_connection(self, connection: Connection) -> None:
        """Close a connection and clean up resources."""
        try:
            await connection.close()
        except Exception as e:
            logger.debug(f"Error closing connection: {e}")
    
    async def close(self) -> None:
        """Close all connections in the pool."""
        # Close all idle connections
        while self._idle_connections:
            conn = self._idle_connections.popleft()
            await self._close_connection(conn)
            
        # Close all active connections
        for conn in list(self._active_connections):
            await self._close_connection(conn)
            
        # Clear the waiting queue
        while not self._waiting_queue.empty():
            future = await self._waiting_queue.get()
            if not future.done():
                future.set_exception(ConnectionError("Connection pool closed"))
    
    def get_idle_connections(self) -> List[Connection]:
        """Get all idle connections in the pool."""
        return list(self._idle_connections)
    
    def get_all_active_connections(self) -> List[Connection]:
        """Get all active connections in the pool."""
        return list(self._active_connections)
    
    def remove_connection(self, connection: Connection) -> None:
        """Remove a connection from the pool without closing it."""
        self._idle_connections = collections.deque(
            conn for conn in self._idle_connections if conn is not connection
        )
        self._active_connections.discard(connection)
    
    @property
    def total_connections(self) -> int:
        """Get the total number of connections in the pool."""
        return len(self._idle_connections) + len(self._active_connections) + self._pending_connections
    
    @property
    def idle_connections(self) -> int:
        """Get the number of idle connections in the pool."""
        return len(self._idle_connections)
    
    @property
    def active_connections(self) -> int:
        """Get the number of active connections in the pool."""
        return len(self._active_connections)


class ConnectionPoolManager:
    """
    Manager for multiple connection pools.
    
    This manages pools for different hosts, handling connection acquisition
    and lifecycle across all of them.
    """
    
    def __init__(
        self,
        max_connections: int = 200,
        max_connections_per_host: int = 20,
        max_keepalive: float = 120,
    ):
        # Host-specific pools (hostname:port → pool)
        self._host_pools: Dict[str, ConnectionPool] = {}
        
        # Pool configuration
        self._max_connections = max_connections
        self._max_connections_per_host = max_connections_per_host
        self._max_keepalive = max_keepalive
        
        # Connection tracking
        self._total_connections = 0
        
    async def get_connection(self, url: str, timeout: float = 10.0) -> Connection:
        """
        Get a connection for the specified URL.
        
        Args:
            url: URL to connect to
            timeout: Maximum time to wait for a connection
            
        Returns:
            Connection object
            
        Raises:
            PoolTimeoutError: If no connection is available within the timeout
        """
        # Parse URL to get host, port, and protocol
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        port = parsed.port or self._get_default_port(parsed.scheme)
        scheme = parsed.scheme
        
        host_key = f"{hostname}:{port}"
        
        # Get or create host-specific pool
        pool = await self._get_or_create_pool(hostname, port, scheme)
        
        # Get connection from host pool
        return await pool.acquire(timeout)
    
    def release_connection(self, connection: Connection, recycle: bool = True) -> None:
        """
        Release a connection back to its pool.
        
        Args:
            connection: Connection to release
            recycle: Whether to recycle the connection or close it
        """
        host_key = connection.host_key
        if host_key in self._host_pools:
            self._host_pools[host_key].release(connection, recycle)
    
    async def _get_or_create_pool(
        self, hostname: str, port: int, scheme: str
    ) -> ConnectionPool:
        """
        Get or create a connection pool for the specified host.
        
        Args:
            hostname: Target hostname
            port: Target port
            scheme: URL scheme (http or https)
            
        Returns:
            ConnectionPool for the host
        """
        host_key = f"{hostname}:{port}"
        
        if host_key not in self._host_pools:
            # Create pool with appropriate settings for this host
            pool = ConnectionPool(
                hostname=hostname,
                port=port,
                scheme=scheme,
                max_connections=self._max_connections_per_host,
                max_keepalive=self._max_keepalive,
            )
            self._host_pools[host_key] = pool
            
        return self._host_pools[host_key]
    
    def _get_default_port(self, scheme: str) -> int:
        """Get the default port for a URL scheme."""
        if scheme == "https":
            return 443
        return 80
    
    async def close(self) -> None:
        """Close all connections and pools."""
        # Close all host pools
        for pool in list(self._host_pools.values()):
            await pool.close()
            
        self._host_pools.clear()
    
    def get_all_pools(self) -> List[ConnectionPool]:
        """Get all connection pools."""
        return list(self._host_pools.values())
    
    @property
    def total_connections(self) -> int:
        """Get the total number of connections across all pools."""
        return sum(pool.total_connections for pool in self._host_pools.values())
    
    @property
    def idle_connections(self) -> int:
        """Get the total number of idle connections across all pools."""
        return sum(pool.idle_connections for pool in self._host_pools.values())
    
    @property
    def active_connections(self) -> int:
        """Get the total number of active connections across all pools."""
        return sum(pool.active_connections for pool in self._host_pools.values())