"""
Base protocol classes for HTTP implementations.
"""

import abc
from typing import Dict, Any, Optional, Union, Tuple

from hyperhttp.utils.buffer_pool import BufferPool


class Protocol(abc.ABC):
    """
    Base class for HTTP protocol implementations.
    
    This defines the interface that all protocol implementations must provide.
    """
    
    @abc.abstractmethod
    async def send_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[Union[bytes, bytearray, memoryview]] = None,
        timeout: float = 30.0,
        buffer_pool: Optional[BufferPool] = None,
    ) -> Dict[str, Any]:
        """
        Send an HTTP request.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: URL to request
            headers: HTTP headers
            body: Request body
            timeout: Request timeout in seconds
            buffer_pool: Buffer pool for memory reuse
            
        Returns:
            Dictionary with response data
        """
        pass
    
    @abc.abstractmethod
    async def close(self) -> None:
        """Close the protocol instance and free resources."""
        pass