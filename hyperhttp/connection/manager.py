"""
Connection lifecycle management.
"""

import asyncio
import logging
import time
from typing import List, Dict, Any, Optional, Set

from hyperhttp.connection.base import Connection
from hyperhttp.connection.pool import ConnectionPool, ConnectionPoolManager

# Logger
logger = logging.getLogger("hyperhttp.connection.manager")


class ConnectionManager:
    """
    Manager for connection lifecycle and health.
    
    This handles periodic cleanup, health checks, and connection pruning
    across all connection pools.
    """
    
    def __init__(
        self,
        pool_manager: ConnectionPoolManager,
        cleanup_interval: float = 30.0,
    ):
        self._pool_manager = pool_manager
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
        
    async def start(self) -> None:
        """Start the connection manager."""
        if self._running:
            return
            
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
    async def stop(self) -> None:
        """Stop the connection manager."""
        if not self._running:
            return
            
        self._running = False
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            
    async def _cleanup_loop(self) -> None:
        """Main cleanup loop that runs periodically."""
        while self._running:
            try:
                await self._cleanup_pools()
                await asyncio.sleep(self._cleanup_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in connection cleanup: {e}")
                
    async def _cleanup_pools(self) -> None:
        """Perform cleanup on all connection pools."""
        now = time.monotonic()
        tasks: List[asyncio.Task] = []
        
        for pool in self._pool_manager.get_all_pools():
            # Remove connections idle for too long
            for conn in pool.get_idle_connections():
                if (conn.metadata.idle_since is not None and
                    now - conn.metadata.idle_since > pool.max_idle_time):
                    pool.remove_connection(conn)
                    tasks.append(asyncio.create_task(conn.close()))
                    continue
                
                # Check if connections are still alive
                if now - conn.metadata.last_checked > pool.health_check_interval:
                    tasks.append(asyncio.create_task(self._health_check(pool, conn)))
                    
        # Wait for all tasks to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
                
    async def _health_check(self, pool: ConnectionPool, connection: Connection) -> None:
        """
        Perform health check on a connection.
        
        Args:
            pool: Connection pool containing the connection
            connection: Connection to check
        """
        try:
            if not await connection.check_health():
                # Connection is unhealthy, remove from pool
                pool.remove_connection(connection)
                await connection.close()
        except Exception:
            # Any error means connection is bad
            pool.remove_connection(connection)
            await connection.close()