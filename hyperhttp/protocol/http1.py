"""
HTTP/1.1 protocol implementation.
"""

import asyncio
import json
import urllib.parse
from typing import Dict, Any, Optional, Union, Tuple, List

from hyperhttp.connection.base import Connection
from hyperhttp.protocol.base import Protocol
from hyperhttp.protocol.utils import (
    parse_url,
    build_request,
    prepare_body,
    parse_headers,
    ChunkedDecoder,
)
from hyperhttp.utils.buffer_pool import BufferPool


class HTTP1Protocol(Protocol):
    """
    HTTP/1.1 protocol implementation.
    
    This handles the details of sending and receiving HTTP/1.1 messages.
    """
    
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False
    
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
        Send an HTTP/1.1 request and receive the response.
        
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
        if self._closed:
            raise ConnectionError("Connection is closed")
        
        # Parse URL to get path
        scheme, hostname, port, path = parse_url(url)
        
        # Prepare headers
        request_headers = {
            "Host": f"{hostname}:{port}" if port not in (80, 443) else hostname,
            "Connection": "keep-alive",
            "User-Agent": "hyperhttp/1.0.0",
        }
        
        if headers:
            request_headers.update(headers)
        
        # Set Content-Length if body is present
        if body:
            request_headers["Content-Length"] = str(len(body))
        
        # Build and send the request
        request_data = build_request(method, path, request_headers, body)
        request_size = len(request_data)
        
        try:
            self._writer.write(request_data)
            await self._writer.drain()
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            self._closed = True
            raise ConnectionError(f"Failed to send request: {e}")
        
        # Receive the response with timeout
        try:
            response = await asyncio.wait_for(
                self._receive_response(buffer_pool),
                timeout=timeout,
            )
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            self._closed = True
            raise ConnectionError(f"Failed to receive response: {e}")
        
        # Add request size to response
        response["request_size"] = request_size
        
        return response
    
    async def _receive_response(
        self, buffer_pool: Optional[BufferPool] = None
    ) -> Dict[str, Any]:
        """
        Receive and parse an HTTP/1.1 response.
        
        Args:
            buffer_pool: Buffer pool for memory reuse
            
        Returns:
            Dictionary with response data
        """
        # Read headers
        header_data = await self._reader.readuntil(b"\r\n\r\n")
        headers, _ = parse_headers(header_data)
        
        status_code = headers.pop("_status_code", 200)
        reason = headers.pop("_reason", "OK")
        
        # Determine how to read the body
        transfer_encoding = headers.get("transfer-encoding", "").lower()
        content_length_str = headers.get("content-length")
        
        body_source = None
        response_size = len(header_data)
        
        if "chunked" in transfer_encoding:
            # Chunked transfer encoding
            decoder = ChunkedDecoder(buffer_pool)
            body_data = await decoder.decode(self._reader)
            response_size += len(body_data)
            body_source = body_data
        elif content_length_str:
            # Content-Length specified
            content_length = int(content_length_str)
            
            if content_length > 0:
                if buffer_pool and content_length > 1024:
                    # Use buffer pool for large responses
                    buffer = bytearray(content_length)
                    view = memoryview(buffer)
                    bytes_read = 0
                    
                    while bytes_read < content_length:
                        chunk = await self._reader.read(min(16384, content_length - bytes_read))
                        if not chunk:
                            break
                        
                        view[bytes_read:bytes_read + len(chunk)] = chunk
                        bytes_read += len(chunk)
                    
                    body_source = bytes(buffer[:bytes_read])
                    response_size += bytes_read
                else:
                    # Direct read for small responses
                    body_data = await self._reader.readexactly(content_length)
                    body_source = body_data
                    response_size += content_length
        else:
            # No Content-Length or chunked encoding
            # For HTTP/1.1, this means the connection will close after the response
            body_data = await self._reader.read()
            body_source = body_data
            response_size += len(body_data)
            self._closed = True
        
        # Check if connection should be closed
        connection = headers.get("connection", "").lower()
        if connection == "close":
            self._closed = True
        
        return {
            "status_code": status_code,
            "reason": reason,
            "headers": headers,
            "body_source": body_source,
            "response_size": response_size,
        }
    
    async def close(self) -> None:
        """Close the protocol."""
        if self._closed:
            return
            
        self._closed = True
        
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            # Ignore errors during close
            pass


class HTTP1Connection(Connection):
    """
    HTTP/1.1 connection implementation.
    
    This manages a socket connection using the HTTP/1.1 protocol.
    """
    
    def __init__(
        self,
        host: str,
        port: int,
        use_tls: bool = False,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        super().__init__(host, port, use_tls, timeout, **kwargs)
        self._protocol: Optional[HTTP1Protocol] = None
    
    async def connect(self) -> None:
        """Establish the connection."""
        await super().connect()
        
        # Create HTTP/1.1 protocol handler
        self._protocol = HTTP1Protocol(self._reader, self._writer)
    
    async def send_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        json: Optional[Any] = None,
        timeout: float = 30.0,
        buffer_pool: Optional[BufferPool] = None,
    ) -> Dict[str, Any]:
        """
        Send an HTTP request over this connection.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: URL to request
            params: Query parameters
            headers: HTTP headers
            data: Request body data
            json: JSON request body
            timeout: Request timeout in seconds
            buffer_pool: Buffer pool for memory reuse
            
        Returns:
            Response object
        """
        if not self._protocol:
            raise ConnectionError("Connection not established")
        
        # Add query parameters if provided
        if params:
            url_parts = list(urllib.parse.urlparse(url))
            query = dict(urllib.parse.parse_qsl(url_parts[4]))
            query.update(params)
            url_parts[4] = urllib.parse.urlencode(query)
            url = urllib.parse.urlunparse(url_parts)
        
        # Prepare request body
        body_bytes, content_type = prepare_body(data, json)
        
        # Add content type header if determined
        request_headers = headers.copy() if headers else {}
        if content_type and "content-type" not in {k.lower(): k for k in request_headers}:
            request_headers["Content-Type"] = content_type
        
        # Send the request
        response = await self._protocol.send_request(
            method=method,
            url=url,
            headers=request_headers,
            body=body_bytes,
            timeout=timeout,
            buffer_pool=buffer_pool,
        )
        
        return response
    
    async def close(self) -> None:
        """Close the connection."""
        if self._protocol:
            await self._protocol.close()
        
        await super().close()
    
    def is_reusable(self) -> bool:
        """Check if the connection can be reused."""
        if not super().is_reusable():
            return False
        
        # Check if protocol was marked as closed
        if self._protocol and getattr(self._protocol, "_closed", False):
            return False
        
        return True