"""
Base connection classes for HTTP client.
"""

import asyncio
import collections
import socket
import ssl
import time
from typing import Dict, Any, Optional, Deque, Tuple, Union, List
import certifi

from hyperhttp.utils.buffer_pool import BufferPool


class ConnectionMetadata:
    """
    Metadata for tracking connection performance and status.
    
    This enables intelligent connection reuse and health tracking.
    """
    
    def __init__(self, connection: "Connection", created_at: Optional[float] = None):
        self.connection = connection
        self.created_at = created_at or time.monotonic()
        self.last_used = self.created_at
        self.idle_since: Optional[float] = None
        self.last_checked = self.created_at
        self.requests_served = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.errors = 0
        self.last_error: Optional[str] = None
        self.marked_for_close = False
        self.rtt_samples: Deque[float] = collections.deque(maxlen=10)  # Round-trip time samples
        
    @property
    def age(self) -> float:
        """Get connection age in seconds."""
        return time.monotonic() - self.created_at
        
    @property
    def idle_time(self) -> float:
        """Get time since connection became idle in seconds."""
        if self.idle_since is None:
            return 0
        return time.monotonic() - self.idle_since
        
    @property
    def average_rtt(self) -> Optional[float]:
        """Get average round-trip time in seconds."""
        if not self.rtt_samples:
            return None
        return sum(self.rtt_samples) / len(self.rtt_samples)
        
    def record_request_success(self, sent_bytes: int, received_bytes: int, rtt: float) -> None:
        """Record successful request metrics."""
        self.requests_served += 1
        self.bytes_sent += sent_bytes
        self.bytes_received += received_bytes
        self.rtt_samples.append(rtt)
        self.last_used = time.monotonic()
        self.idle_since = time.monotonic()
        
    def record_request_failure(self, error: str) -> None:
        """Record request failure."""
        self.errors += 1
        self.last_error = error
        self.last_used = time.monotonic()
        self.idle_since = time.monotonic()


class Connection:
    """
    Base connection class for HTTP clients.
    
    This handles the low-level socket operations and connection management.
    """
    
    def __init__(
        self,
        host: str,
        port: int,
        use_tls: bool = False,
        timeout: float = 30.0,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._timeout = timeout
        self._ssl_context = ssl_context or self._create_default_ssl_context()
        
        self._socket = None
        self._reader = None
        self._writer = None
        self._closed = False
        self._protocol = None
        
        # Metadata for tracking connection health and performance
        self.metadata = ConnectionMetadata(self)
        
        # Key for connection pooling
        self.host_key = f"{host}:{port}"
        
    async def connect(self) -> None:
        """Establish the connection."""
        if self._closed:
            raise ConnectionError("Connection is closed")
            
        try:
            # Use high-level asyncio API to establish the connection
            if self._use_tls:
                # For TLS connections
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=self._host,
                        port=self._port,
                        ssl=self._ssl_context,
                        ssl_handshake_timeout=self._timeout
                    ),
                    timeout=self._timeout
                )
            else:
                # For plain connections
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=self._host,
                        port=self._port
                    ),
                    timeout=self._timeout
                )
                
            # Get the socket from the transport
            transport = self._writer.transport
            self._socket = transport.get_extra_info('socket')
            
            # Configure socket options
            if self._socket:
                self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # Disable Nagle's algorithm
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)  # Enable keepalive
                
        except asyncio.TimeoutError:
            raise TimeoutError(f"Connection to {self._host}:{self._port} timed out")
        except (OSError, ssl.SSLError) as e:
            raise ConnectionError(f"Failed to connect to {self._host}:{self._port}: {e}")
    
    
    def _create_default_ssl_context(self) -> ssl.SSLContext:
        """Create a default SSL context with good security settings."""
        context = ssl.create_default_context(
            cafile=certifi.where(),
            capath=certifi.where()
        )
        context.set_alpn_protocols(["h2", "http/1.1"])
        context.options |= ssl.OP_NO_COMPRESSION  # Disable compression (CRIME attack)
        return context
    
    async def send_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Union[Dict[str, Any], str, bytes]] = None,
        json: Optional[Any] = None,
        timeout: float = 30.0,
        buffer_pool: Optional[BufferPool] = None,
    ) -> Any:
        """
        Send an HTTP request over this connection.
        
        This is implemented by protocol-specific subclasses.
        """
        raise NotImplementedError("Subclasses must implement send_request")
    
    async def check_health(self) -> bool:
        """
        Check if the connection is still healthy.
        
        Returns:
            True if connection is healthy, False otherwise
        """
        if self._closed:
            return False
            
        try:
            # Cheapest possible health check: see if socket is still connected
            if self._socket.fileno() == -1:
                return False
                
            # Update last checked timestamp
            self.metadata.last_checked = time.monotonic()
            return True
        except Exception:
            return False
    
    async def close(self) -> None:
        """Close the connection."""
        if self._closed:
            return
            
        self._closed = True
        
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            
        if self._socket and self._socket.fileno() != -1:
            try:
                self._socket.close()
            except Exception:
                pass
            
        self._socket = None
        self._reader = None
        self._writer = None
    
    def is_reusable(self) -> bool:
        """
        Check if the connection can be reused.
        
        Returns:
            True if the connection can be reused, False otherwise
        """
        if self._closed or self.metadata.marked_for_close:
            return False
            
        # Check socket is still valid
        if not self._socket or self._socket.fileno() == -1:
            return False
            
        # Connection too old? (typically 5-15 minutes depending on server)
        max_age = 600  # 10 minutes
        if self.metadata.age > max_age:
            return False
            
        # Too many errors?
        if self.metadata.errors > 5:
            return False
            
        return True