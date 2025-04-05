"""
HTTP/2 protocol implementation.
"""

import asyncio
import logging
import ssl
import urllib.parse
from typing import Dict, Any, Optional, Union, Tuple, List, Set, DefaultDict, Deque
import collections

# Use the h2 library for HTTP/2 protocol implementation
import h2.connection
import h2.events
import h2.settings

from hyperhttp.connection.base import Connection
from hyperhttp.protocol.base import Protocol
from hyperhttp.protocol.utils import parse_url, prepare_body
from hyperhttp.utils.buffer_pool import BufferPool

# Logger
logger = logging.getLogger("hyperhttp.protocol.http2")


class HTTP2Stream:
    """
    HTTP/2 stream state tracking.
    
    This tracks the state and collects data for a single HTTP/2 stream.
    """
    
    def __init__(self, stream_id: int):
        self.stream_id = stream_id
        self.headers: Dict[str, str] = {}
        self.status_code: Optional[int] = None
        self.data_chunks: List[bytes] = []
        self.complete = False
        self.error: Optional[Exception] = None
        self.data_received = asyncio.Event()
        
    def add_headers(self, headers: Dict[bytes, bytes]) -> None:
        """
        Add response headers.
        
        Args:
            headers: Headers as byte pairs
        """
        # Decode header values
        for name, value in headers.items():
            name_str = name.decode("ascii").lower()
            value_str = value.decode("latin1")
            
            if name_str == ":status":
                self.status_code = int(value_str)
            else:
                # Remove pseudo-headers
                if not name_str.startswith(":"):
                    self.headers[name_str] = value_str
    
    def add_data(self, data: bytes) -> None:
        """
        Add response body data.
        
        Args:
            data: Body chunk
        """
        if data:
            self.data_chunks.append(data)
        self.data_received.set()
    
    def mark_complete(self) -> None:
        """Mark the stream as complete."""
        self.complete = True
        self.data_received.set()
    
    def set_error(self, error: Exception) -> None:
        """
        Set an error on the stream.
        
        Args:
            error: Exception that occurred
        """
        self.error = error
        self.data_received.set()
    
    async def wait_for_data(self) -> None:
        """Wait for data to be received."""
        await self.data_received.wait()
        self.data_received.clear()
    
    def get_body(self) -> bytes:
        """
        Get the complete response body.
        
        Returns:
            Body as bytes
        """
        if not self.data_chunks:
            return b""
        if len(self.data_chunks) == 1:
            return self.data_chunks[0]
        return b"".join(self.data_chunks)
    
    def reset(self) -> None:
        """Reset the stream state for reuse."""
        self.headers.clear()
        self.status_code = None
        self.data_chunks.clear()
        self.complete = False
        self.error = None
        self.data_received.clear()


class HTTP2Protocol(Protocol):
    """
    HTTP/2 protocol implementation.
    
    This handles the details of the HTTP/2 protocol, including establishing
    the connection, managing streams, and handling the protocol state.
    """
    
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ssl_context: Optional[ssl.SSLContext] = None):
        self._reader = reader
        self._writer = writer
        self._ssl_context = ssl_context
        
        # Get the SSL object for ALPN protocol information
        self._ssl_object = writer.get_extra_info("ssl_object")
        
        # Create H2 connection
        self._conn = h2.connection.H2Connection()
        
        # Stream tracking
        self._streams: Dict[int, HTTP2Stream] = {}
        self._next_stream_id = 1
        self._max_concurrent_streams = 100  # Initial guess, will be updated from settings
        
        # State
        self._initialized = False
        self._closed = False
        self._error: Optional[Exception] = None
        
        # Background reader task
        self._read_task: Optional[asyncio.Task] = None
    
    async def initialize(self) -> None:
        """Initialize the HTTP/2 connection."""
        if self._initialized:
            return
        
        # Check ALPN protocol if available
        if self._ssl_object:
            protocol = self._ssl_object.selected_alpn_protocol()
            if protocol != "h2":
                # Server doesn't support HTTP/2 via ALPN
                raise ConnectionError(
                    f"Server doesn't support HTTP/2 via ALPN (got {protocol})"
                )
        
        # Start the connection
        self._conn.initiate_connection()
        data = self._conn.data_to_send()
        
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as e:
            raise ConnectionError(f"Failed to initialize HTTP/2 connection: {e}")
        
        # Start background reader task
        self._read_task = asyncio.create_task(self._reader_loop())
        
        # Mark as initialized
        self._initialized = True
    
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
        Send an HTTP/2 request and receive the response.
        
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
        
        # Initialize if not already done
        if not self._initialized:
            await self.initialize()
        
        # Parse URL
        scheme, hostname, port, path = parse_url(url)
        
        # Prepare headers
        request_headers = [
            (":method", method),
            (":scheme", scheme),
            (":authority", f"{hostname}:{port}" if port not in (80, 443) else hostname),
            (":path", path),
            ("user-agent", "hyperhttp/1.0.0"),
        ]
        
        # Add custom headers
        if headers:
            for name, value in headers.items():
                # Skip pseudo-headers
                if name.lower().startswith(":"):
                    continue
                request_headers.append((name.lower(), value))
        
        # Get stream ID
        stream_id = self._get_next_stream_id()
        
        # Create stream tracking object
        stream = HTTP2Stream(stream_id)
        self._streams[stream_id] = stream
        
        # Send headers
        self._conn.send_headers(
            stream_id=stream_id,
            headers=request_headers,
            end_stream=body is None,
        )
        
        # Send body if present
        if body:
            if isinstance(body, (bytes, bytearray)):
                self._conn.send_data(
                    stream_id=stream_id,
                    data=body,
                    end_stream=True,
                )
            elif isinstance(body, memoryview):
                self._conn.send_data(
                    stream_id=stream_id,
                    data=body.tobytes(),
                    end_stream=True,
                )
        
        # Send pending data
        data = self._conn.data_to_send()
        if data:
            self._writer.write(data)
            await self._writer.drain()
        
        # Calculate request size (approximate)
        request_size = sum(len(name) + len(value) for name, value in request_headers)
        if body:
            request_size += len(body)
        
        # Wait for response with timeout
        try:
            return await asyncio.wait_for(
                self._wait_for_response(stream),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Cancel stream
            self._conn.reset_stream(stream_id)
            data = self._conn.data_to_send()
            if data:
                self._writer.write(data)
                await self._writer.drain()
            
            raise TimeoutError(f"Request timed out after {timeout} seconds")
        finally:
            # Clean up stream
            self._streams.pop(stream_id, None)
    
    async def _wait_for_response(self, stream: HTTP2Stream) -> Dict[str, Any]:
        """
        Wait for a complete response on a stream.
        
        Args:
            stream: Stream to wait for
            
        Returns:
            Response data dictionary
        """
        # Wait for initial headers
        while not stream.status_code:
            if stream.error:
                raise stream.error
            
            if self._error:
                raise self._error
                
            await stream.wait_for_data()
        
        # Wait for complete response
        while not stream.complete:
            if stream.error:
                raise stream.error
                
            if self._error:
                raise self._error
                
            await stream.wait_for_data()
        
        # Get response body
        body_data = stream.get_body()
        
        return {
            "status_code": stream.status_code,
            "reason": "",  # HTTP/2 doesn't have reason phrases
            "headers": stream.headers,
            "body_source": body_data,
            "response_size": len(body_data) + sum(len(k) + len(v) for k, v in stream.headers.items()),
        }
    
    def _get_next_stream_id(self) -> int:
        """
        Get the next stream ID.
        
        Returns:
            Stream ID for a new request
        """
        # HTTP/2 client-initiated streams are odd-numbered
        stream_id = self._next_stream_id
        self._next_stream_id += 2
        return stream_id
    
    async def _reader_loop(self) -> None:
        """Background reader loop for HTTP/2 connection."""
        try:
            while not self._closed:
                data = await self._reader.read(65535)
                if not data:
                    # Connection closed
                    self._closed = True
                    self._error = ConnectionError("Connection closed by peer")
                    break
                
                events = self._conn.receive_data(data)
                
                # Process events
                for event in events:
                    self._process_event(event)
                
                # Send any pending data
                data = self._conn.data_to_send()
                if data:
                    self._writer.write(data)
                    await self._writer.drain()
        except Exception as e:
            self._closed = True
            self._error = e
            
            # Mark all streams as errored
            for stream in self._streams.values():
                stream.set_error(e)
        finally:
            # Ensure all streams are marked complete
            for stream in self._streams.values():
                if not stream.complete and not stream.error:
                    stream.set_error(
                        ConnectionError("Connection closed before response completed")
                    )
    
    def _process_event(self, event: h2.events.Event) -> None:
        """
        Process an HTTP/2 event.
        
        Args:
            event: Event to process
        """
        if isinstance(event, h2.events.ResponseReceived):
            # Headers received
            stream = self._streams.get(event.stream_id)
            if stream:
                stream.add_headers(dict(event.headers))
        
        elif isinstance(event, h2.events.DataReceived):
            # Data received
            stream = self._streams.get(event.stream_id)
            if stream:
                stream.add_data(event.data)
                
                # Acknowledge data (flow control)
                self._conn.acknowledge_received_data(
                    event.flow_controlled_length, event.stream_id
                )
        
        elif isinstance(event, h2.events.StreamEnded):
            # Stream complete
            stream = self._streams.get(event.stream_id)
            if stream:
                stream.mark_complete()
        
        elif isinstance(event, h2.events.StreamReset):
            # Stream reset by server
            stream = self._streams.get(event.stream_id)
            if stream:
                stream.set_error(
                    ConnectionError(f"Stream reset by server: {event.error_code}")
                )
        
        elif isinstance(event, h2.events.RemoteSettingsChanged):
            # Settings changed
            if hasattr(event, "max_concurrent_streams"):
                self._max_concurrent_streams = event.max_concurrent_streams
        
        elif isinstance(event, h2.events.ConnectionTerminated):
            # Connection terminated
            self._closed = True
            self._error = ConnectionError(
                f"Connection terminated by server: {event.error_code}"
            )
    
    @property
    def available_streams(self) -> int:
        """
        Get the number of available streams.
        
        Returns:
            Number of streams that can be opened
        """
        return self._max_concurrent_streams - len(self._streams)
    
    async def close(self) -> None:
        """Close the connection."""
        if self._closed:
            return
            
        self._closed = True
        
        # Cancel reader task
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        
        # Send goaway frame
        try:
            self._conn.close_connection()
            data = self._conn.data_to_send()
            if data:
                self._writer.write(data)
                await self._writer.drain()
        except Exception:
            # Ignore errors during close
            pass
        
        # Close writer
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            # Ignore errors during close
            pass


class HTTP2Connection(Connection):
    """
    HTTP/2 connection implementation.
    
    This manages a socket connection using the HTTP/2 protocol.
    """
    
    def __init__(
        self,
        host: str,
        port: int,
        use_tls: bool = True,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        super().__init__(host, port, use_tls, timeout, **kwargs)
        self._protocol: Optional[HTTP2Protocol] = None
    
    async def connect(self) -> None:
        """Establish the connection."""
        # HTTP/2 requires TLS or an upgrade
        if not self._use_tls:
            logger.warning("HTTP/2 without TLS is not fully supported yet")
        
        # Ensure we're using an SSL context with ALPN for HTTP/2
        if self._use_tls and not self._ssl_context:
            self._ssl_context = self._create_default_ssl_context()
        
        await super().connect()
        
        # Create HTTP/2 protocol handler
        self._protocol = HTTP2Protocol(self._reader, self._writer, self._ssl_context)
        
        # Initialize the HTTP/2 connection
        await self._protocol.initialize()
    
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
        
        # Check if protocol has available streams
        if self._protocol and hasattr(self._protocol, "available_streams"):
            return self._protocol.available_streams > 0
        
        return True
    
    @property
    def available_streams(self) -> int:
        """
        Get the number of available streams.
        
        Returns:
            Number of streams that can be opened
        """
        if self._protocol:
            return self._protocol.available_streams
        return 0