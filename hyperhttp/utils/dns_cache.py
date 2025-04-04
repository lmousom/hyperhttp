"""
DNS caching utilities to minimize DNS resolution overhead.
"""

import asyncio
import logging
import socket
import time
from typing import Dict, List, Tuple, Any, Optional, Set

# Logger
logger = logging.getLogger("hyperhttp.utils.dns_cache")


class DNSCache:
    """
    Cache for DNS resolution results.
    
    This cache reduces DNS lookup overhead by storing resolution results
    and respecting TTLs.
    """
    
    def __init__(self, ttl: float = 300.0):
        """
        Initialize DNS cache.
        
        Args:
            ttl: Default time-to-live for cache entries in seconds
        """
        self._cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()
        
    async def resolve(
        self,
        hostname: str,
        port: int,
    ) -> List[Dict[str, Any]]:
        """
        Resolve a hostname to addresses.
        
        Args:
            hostname: Hostname to resolve
            port: Port number
            
        Returns:
            List of address information dictionaries
        """
        cache_key = (hostname, port)
        
        # Check cache first
        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry and entry['expiry'] > time.monotonic():
                logger.debug(f"DNS cache hit for {hostname}:{port}")
                return entry['addresses']
                
        # Cache miss, do actual DNS resolution
        logger.debug(f"DNS cache miss for {hostname}:{port}")
        addresses = await self._do_dns_lookup(hostname, port)
        
        # Update cache
        async with self._lock:
            self._cache[cache_key] = {
                'addresses': addresses,
                'expiry': time.monotonic() + self._ttl
            }
            
        return addresses
        
    async def _do_dns_lookup(
        self,
        hostname: str,
        port: int,
    ) -> List[Dict[str, Any]]:
        """
        Perform actual DNS resolution.
        
        Args:
            hostname: Hostname to resolve
            port: Port number
            
        Returns:
            List of address information dictionaries
        """
        # Use getaddrinfo for proper dual-stack IPv4/IPv6 handling
        loop = asyncio.get_event_loop()
        try:
            infos = await loop.getaddrinfo(
                hostname, port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as e:
            logger.error(f"DNS resolution failed for {hostname}: {e}")
            raise
        
        # Extract address info
        addresses = []
        for family, socktype, proto, canonname, sockaddr in infos:
            addresses.append({
                'family': family,
                'sockaddr': sockaddr,
                'socktype': socktype,
                'proto': proto,
            })
            
        return addresses
    
    async def clear(self) -> None:
        """Clear the entire cache."""
        async with self._lock:
            self._cache.clear()
            
    async def remove(self, hostname: str, port: int) -> None:
        """
        Remove a specific entry from the cache.
        
        Args:
            hostname: Hostname to remove
            port: Port number
        """
        async with self._lock:
            self._cache.pop((hostname, port), None)
            
    async def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dictionary of cache statistics
        """
        async with self._lock:
            now = time.monotonic()
            return {
                'total_entries': len(self._cache),
                'valid_entries': sum(1 for entry in self._cache.values() 
                                    if entry['expiry'] > now),
                'expired_entries': sum(1 for entry in self._cache.values() 
                                      if entry['expiry'] <= now),
            }


class DNSResolver:
    """
    Resolver for optimized DNS lookups.
    
    This includes connection racing across multiple addresses to find
    the fastest path to a host.
    """
    
    def __init__(self, cache: Optional[DNSCache] = None):
        """
        Initialize DNS resolver.
        
        Args:
            cache: Optional DNS cache to use
        """
        self._cache = cache or DNSCache()
        
    async def resolve(
        self,
        hostname: str,
        port: int,
    ) -> List[Dict[str, Any]]:
        """
        Resolve a hostname to addresses.
        
        Args:
            hostname: Hostname to resolve
            port: Port number
            
        Returns:
            List of address information dictionaries
        """
        return await self._cache.resolve(hostname, port)
        
    async def race_connection(
        self,
        hostname: str,
        port: int,
        timeout: float = 5.0,
        connection_factory: Any = None,
    ) -> Any:
        """
        Race connections to multiple addresses to find the fastest.
        
        Args:
            hostname: Hostname to connect to
            port: Port number
            timeout: Connection timeout in seconds
            connection_factory: Factory function for creating connections
            
        Returns:
            The winning connection
        """
        if connection_factory is None:
            raise ValueError("connection_factory is required")
            
        # Get all possible addresses
        addresses = await self._cache.resolve(hostname, port)
        
        if not addresses:
            raise ConnectionError(f"Could not resolve {hostname}")
            
        # Create a future to represent the winning connection
        winner = asyncio.Future()
        
        # Create connections to each address in parallel
        tasks = []
        for addr in addresses:
            task = asyncio.create_task(
                self._connect_and_set_winner(
                    addr, hostname, port, winner, connection_factory
                )
            )
            tasks.append(task)
            
        try:
            # Wait for the first successful connection or timeout
            return await asyncio.wait_for(winner, timeout)
        finally:
            # Cancel all other connection attempts
            for task in tasks:
                if not task.done():
                    task.cancel()
                    
    async def _connect_and_set_winner(
        self,
        addr: Dict[str, Any],
        hostname: str,
        port: int,
        winner: asyncio.Future,
        connection_factory: Any,
    ) -> None:
        """
        Connect to a specific address and set as winner if first to succeed.
        
        Args:
            addr: Address information dictionary
            hostname: Hostname for connection
            port: Port number
            winner: Future to set with winning connection
            connection_factory: Factory function for creating connections
        """
        try:
            # Create connection to specific address
            conn = await connection_factory(
                hostname, port, 
                family=addr['family'],
                sockaddr=addr['sockaddr'],
            )
            
            # Set as winner if not already won
            if not winner.done():
                winner.set_result(conn)
        except Exception as e:
            # Connection failed, only propagate if no winners
            if not winner.done() and all(t.done() for t in asyncio.all_tasks() 
                                       if t != asyncio.current_task()):
                winner.set_exception(e)