import asyncio
import pytest
from hyperhttp.connection.pool import (
    ConnectionPool,
    ConnectionPoolManager,
    PoolTimeoutError,
)
from hyperhttp.connection.base import Connection, ConnectionMetadata
import time

class MockConnection:
    def __init__(self, is_healthy=True, is_reusable=True):
        self.is_healthy = is_healthy
        self._is_reusable = is_reusable
        self.closed = False
        self.connect_called = False
        self.check_health_called = False
        # Initialize metadata with self as the connection
        self.metadata = ConnectionMetadata(self)
        self.metadata.marked_for_close = False
        self.metadata.last_used = time.monotonic()
        self.metadata.idle_since = None
        self.host_key = "example.com:443"  # For pool manager tests

    async def connect(self):
        self.connect_called = True

    async def close(self):
        self.closed = True

    def is_reusable(self):
        return self._is_reusable

    async def check_health(self):
        self.check_health_called = True
        return self.is_healthy

@pytest.fixture
def mock_connection_factory(monkeypatch):
    def factory():
        return MockConnection()
    return factory

@pytest.fixture
def pool():
    pool = ConnectionPool(
        hostname="example.com",
        port=443,
        scheme="https",
        max_keepalive=60 
    )
    return pool

@pytest.fixture
def pool_manager():
    return ConnectionPoolManager()

class TestConnectionPool:
    @pytest.mark.asyncio
    async def test_acquire_new_connection(self, pool, mock_connection_factory):
        pool._factory = mock_connection_factory
        conn = await pool.acquire()
        
        assert isinstance(conn, MockConnection)
        assert conn.connect_called
        assert len(pool._active_connections) == 1
        assert len(pool._idle_connections) == 0

    @pytest.mark.asyncio
    async def test_acquire_reuse_idle_connection(self, pool, mock_connection_factory):
        pool._factory = mock_connection_factory
        
        # Create and release a connection
        conn1 = await pool.acquire()
        pool.release(conn1)
        
        # Acquire again - should reuse the connection
        conn2 = await pool.acquire()
        assert conn1 is conn2
        assert len(pool._active_connections) == 1
        assert len(pool._idle_connections) == 0

    @pytest.mark.asyncio
    async def test_acquire_timeout(self, pool):
        # Fill the pool to max capacity
        pool._max_connections = 1
        conn = await pool.acquire()
        
        # Try to acquire another connection
        with pytest.raises(PoolTimeoutError):
            await pool.acquire(timeout=0.1)

    @pytest.mark.asyncio
    async def test_release_connection(self, pool, mock_connection_factory):
        pool._factory = mock_connection_factory
        
        conn = await pool.acquire()
        pool.release(conn)
        
        assert len(pool._active_connections) == 0
        assert len(pool._idle_connections) == 1

    @pytest.mark.asyncio
    async def test_release_non_reusable_connection(self, pool):
        conn = MockConnection(is_reusable=False)
        pool._active_connections.add(conn)
        
        pool.release(conn, recycle=True)
        await asyncio.sleep(0)  # Let the close task run
        
        assert conn.closed
        assert len(pool._active_connections) == 0
        assert len(pool._idle_connections) == 0

    @pytest.mark.asyncio
    async def test_validate_connection(self, pool):
        conn = MockConnection(is_healthy=True)
        # Set last validation time to force health check
        pool._last_validation_time = 0
        # Set last used time to be old enough to trigger validation
        conn.metadata.last_used = time.monotonic() - pool.max_idle_time - 1
        is_valid = await pool._validate_connection(conn)
        
        assert is_valid
        assert conn.check_health_called

    @pytest.mark.asyncio
    async def test_validate_unhealthy_connection(self, pool):
        conn = MockConnection(is_healthy=False)
        # Set last validation time to force health check
        pool._last_validation_time = 0
        # Set last used time to be old enough to trigger validation
        conn.metadata.last_used = time.monotonic() - pool.max_idle_time - 1
        is_valid = await pool._validate_connection(conn)
        
        assert not is_valid
        assert conn.check_health_called

class TestConnectionPoolManager:
    @pytest.mark.asyncio
    async def test_get_connection(self, pool_manager):
        conn = await pool_manager.get_connection("https://example.com/test")
        assert isinstance(conn, Connection)
        
        # Should create a pool for the host
        assert len(pool_manager._host_pools) == 1
        assert "example.com:443" in pool_manager._host_pools

    @pytest.mark.asyncio
    async def test_release_connection(self, pool_manager):
        conn = await pool_manager.get_connection("https://example.com/test")
        pool_manager.release_connection(conn)
        
        pool = pool_manager._host_pools["example.com:443"]
        assert len(pool._active_connections) == 0
        assert len(pool._idle_connections) == 1

    @pytest.mark.asyncio
    async def test_connection_limits(self, pool_manager):
        # Set very low limits for testing
        pool_manager._max_connections_per_host = 2
        
        # Acquire max connections
        conn1 = await pool_manager.get_connection("https://example.com/test")
        conn2 = await pool_manager.get_connection("https://example.com/test")
        
        # Third connection should timeout
        with pytest.raises(PoolTimeoutError):
            await pool_manager.get_connection("https://example.com/test", timeout=0.1)

    @pytest.mark.asyncio
    async def test_multiple_hosts(self, pool_manager):
        conn1 = await pool_manager.get_connection("https://example1.com/test")
        conn2 = await pool_manager.get_connection("https://example2.com/test")
        
        assert len(pool_manager._host_pools) == 2
        assert "example1.com:443" in pool_manager._host_pools
        assert "example2.com:443" in pool_manager._host_pools

    @pytest.mark.asyncio
    async def test_close_all_pools(self, pool_manager):
        await pool_manager.get_connection("https://example1.com/test")
        await pool_manager.get_connection("https://example2.com/test")
        
        await pool_manager.close()
        
        assert len(pool_manager._host_pools) == 0

    def test_connection_stats(self, pool_manager):
        # Add some mock pools with known connection counts
        pool1 = ConnectionPool("example1.com", 443, "https")
        pool2 = ConnectionPool("example2.com", 443, "https")
        
        # Add some mock connections
        pool1._active_connections.add(MockConnection())
        pool1._idle_connections.append(MockConnection())
        pool2._active_connections.add(MockConnection())
        
        pool_manager._host_pools = {
            "example1.com:443": pool1,
            "example2.com:443": pool2
        }
        
        assert pool_manager.total_connections == 3
        assert pool_manager.active_connections == 2
        assert pool_manager.idle_connections == 1
