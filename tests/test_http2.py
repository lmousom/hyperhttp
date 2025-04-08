import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock, call
import ssl
import urllib.parse

import pytest
import h2.connection
import h2.events
import h2.settings
import h2.errors
import h2.exceptions
import h2.config

from hyperhttp.protocol.http2 import HTTP2Protocol, HTTP2Stream
from hyperhttp.utils.buffer_pool import BufferPool


class TestHTTP2Stream:
    def setup_method(self):
        self.stream = HTTP2Stream(stream_id=1)
        
    def test_add_headers(self):
        """Test adding headers to a stream."""
        headers = {
            b':status': b'200',
            b'content-type': b'application/json',
            b'content-length': b'42',
            b':invalid': b'pseudo-header'
        }
        
        self.stream.add_headers(headers)
        
        # Check status code was extracted
        assert self.stream.status_code == 200
        
        # Check normal headers were added
        assert self.stream.headers['content-type'] == 'application/json'
        assert self.stream.headers['content-length'] == '42'
        
        # Check pseudo-header was filtered
        assert ':invalid' not in self.stream.headers
        
    def test_add_data(self):
        """Test adding data to a stream."""
        # Initial state
        assert not self.stream.data_received.is_set()
        
        # Add data
        self.stream.add_data(b'chunk1')
        
        # Check data was added and event was set
        assert self.stream.data_chunks == [b'chunk1']
        assert self.stream.data_received.is_set()
        
        # Reset event and add more data
        self.stream.data_received.clear()
        self.stream.add_data(b'chunk2')
        
        # Check data was added and event was set again
        assert self.stream.data_chunks == [b'chunk1', b'chunk2']
        assert self.stream.data_received.is_set()
        
        # Empty data should not add to chunks but still set event
        self.stream.data_received.clear()
        self.stream.add_data(b'')
        assert self.stream.data_chunks == [b'chunk1', b'chunk2']
        assert self.stream.data_received.is_set()
        
    def test_mark_complete(self):
        """Test marking a stream as complete."""
        # Initial state
        assert not self.stream.complete
        assert not self.stream.data_received.is_set()
        
        # Mark complete
        self.stream.mark_complete()
        
        # Check state
        assert self.stream.complete
        assert self.stream.data_received.is_set()
        
    def test_set_error(self):
        """Test setting an error on a stream."""
        # Initial state
        assert self.stream.error is None
        assert not self.stream.data_received.is_set()
        
        # Set error
        error = ValueError("Test error")
        self.stream.set_error(error)
        
        # Check state
        assert self.stream.error == error
        assert self.stream.data_received.is_set()
        
    @pytest.mark.asyncio
    async def test_wait_for_data(self):
        """Test waiting for data on a stream."""
        # Schedule a task to set data received after a delay
        async def set_data_received():
            await asyncio.sleep(0.01)
            self.stream.data_received.set()
            
        # Start task
        task = asyncio.create_task(set_data_received())
        
        try:
            # Wait for data (should block until data_received is set)
            await self.stream.wait_for_data()
            
            # Check event was cleared
            assert not self.stream.data_received.is_set()
        finally:
            # Clean up
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
    def test_get_body(self):
        """Test getting the complete body from a stream."""
        # Empty body
        assert self.stream.get_body() == b''
        
        # Single chunk
        self.stream.data_chunks = [b'single chunk']
        assert self.stream.get_body() == b'single chunk'
        
        # Multiple chunks
        self.stream.data_chunks = [b'chunk1', b'chunk2', b'chunk3']
        assert self.stream.get_body() == b'chunk1chunk2chunk3'
        
    def test_reset(self):
        """Test resetting a stream."""
        # Setup stream with data
        self.stream.status_code = 200
        self.stream.headers = {'content-type': 'application/json'}
        self.stream.data_chunks = [b'test data']
        self.stream.complete = True
        self.stream.error = ValueError("Test error")
        self.stream.data_received.set()
        
        # Reset
        self.stream.reset()
        
        # Check state was reset
        assert self.stream.status_code is None
        assert self.stream.headers == {}
        assert self.stream.data_chunks == []
        assert not self.stream.complete
        assert self.stream.error is None
        assert not self.stream.data_received.is_set()


class TestHTTP2Protocol:
    def setup_method(self):
        # Mock dependencies
        self.reader = AsyncMock(spec=asyncio.StreamReader)
        self.writer = MagicMock(spec=asyncio.StreamWriter)
        self.ssl_context = Mock(spec=ssl.SSLContext)
        
        # For SSL protocol info
        self.ssl_object = Mock()
        self.ssl_object.selected_alpn_protocol.return_value = "h2"
        self.writer.get_extra_info.return_value = self.ssl_object
        
        # Create protocol with mocked dependencies
        self.protocol = HTTP2Protocol(self.reader, self.writer, self.ssl_context)
        
        # Mock the h2 connection
        self.h2_conn = Mock(spec=h2.connection.H2Connection)
        self.protocol._conn = self.h2_conn
        
    @pytest.mark.asyncio
    async def test_initialize(self):
        """Test initializing the HTTP/2 connection."""
        # Setup
        self.h2_conn.data_to_send.return_value = b'client preface'
        
        # Execute
        await self.protocol.initialize()
        
        # Verify
        self.h2_conn.initiate_connection.assert_called_once()
        self.h2_conn.data_to_send.assert_called_once()
        self.writer.write.assert_called_once_with(b'client preface')
        self.writer.drain.assert_called_once()
        assert self.protocol._initialized
        
        # Calling initialize again should be a no-op
        self.h2_conn.reset_mock()
        self.writer.reset_mock()
        
        await self.protocol.initialize()
        
        self.h2_conn.initiate_connection.assert_not_called()
        self.writer.write.assert_not_called()
        
    @pytest.mark.asyncio
    async def test_initialize_alpn_error(self):
        """Test error when server doesn't support HTTP/2 via ALPN."""
        # Setup - server responds with h1 instead of h2
        self.ssl_object.selected_alpn_protocol.return_value = "http/1.1"
        
        # Execute and verify
        with pytest.raises(ConnectionError) as exc_info:
            await self.protocol.initialize()
            
        assert "doesn't support HTTP/2 via ALPN" in str(exc_info.value)
        
    @pytest.mark.asyncio
    async def test_initialize_connection_error(self):
        """Test error during HTTP/2 connection initialization."""
        # Setup - drain raises an error
        self.h2_conn.data_to_send.return_value = b'client preface'
        self.writer.drain.side_effect = ConnectionError("Connection refused")
        
        # Execute and verify
        with pytest.raises(ConnectionError) as exc_info:
            await self.protocol.initialize()
            
        assert "Failed to initialize HTTP/2 connection" in str(exc_info.value)
        
    @pytest.mark.asyncio
    async def test_send_request(self):
        """Test sending an HTTP/2 request."""
        # Setup
        self.protocol._initialized = True
        self.h2_conn.data_to_send.return_value = b'request data'
        
        # Create a wait_for_response method that returns immediately
        response_data = {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body_source": b'{"result": "ok"}',
            "response_size": 100
        }
        
        with patch.object(
            self.protocol, '_wait_for_response', AsyncMock(return_value=response_data)
        ):
            # Execute
            result = await self.protocol.send_request(
                method="GET",
                url="https://example.com/api/resource",
                headers={"Authorization": "Bearer token"},
                body=b'request body',
                timeout=10.0
            )
            
            # Verify
            assert result == response_data
            
            # Check headers were sent correctly
            headers_call = self.h2_conn.send_headers.call_args[1]
            assert headers_call["stream_id"] == 1
            assert not headers_call["end_stream"]
            
            sent_headers = dict(headers_call["headers"])
            assert sent_headers[":method"] == "GET"
            assert sent_headers[":scheme"] == "https"
            assert sent_headers[":authority"] == "example.com"
            assert sent_headers[":path"] == "/api/resource"
            assert sent_headers["authorization"] == "Bearer token"
            
            # Check body was sent
            self.h2_conn.send_data.assert_called_once()
            data_call = self.h2_conn.send_data.call_args[1]
            assert data_call["stream_id"] == 1
            assert data_call["data"] == b'request body'
            assert data_call["end_stream"]
            
            # Check data was written to the writer
            self.writer.write.assert_called_once_with(b'request data')
            self.writer.drain.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_request_with_memoryview(self):
        """Test sending a request with memoryview body."""
        # Setup
        self.protocol._initialized = True
        self.h2_conn.data_to_send.return_value = b'request data'
        
        # Mock wait_for_response
        mock_response = {
            "status_code": 200,
            "headers": {},
            "body_source": b'',
            "response_size": 0
        }
        with patch.object(
            self.protocol, '_wait_for_response', AsyncMock(return_value=mock_response)
        ):
            # Create a memoryview body
            body_bytes = b'memory view body'
            body_view = memoryview(body_bytes)
            
            # Execute
            await self.protocol.send_request(
                method="POST",
                url="https://example.com/api",
                body=body_view
            )
            
            # Verify the body was converted from memoryview to bytes
            data_call = self.h2_conn.send_data.call_args[1]
            assert data_call["data"] == body_bytes
    
    @pytest.mark.asyncio
    async def test_send_request_timeout(self):
        """Test request timeout handling."""
        # Setup
        self.protocol._initialized = True
        
        # Mock wait_for_response to timeout
        with patch.object(
            self.protocol, '_wait_for_response', AsyncMock(side_effect=asyncio.TimeoutError())
        ):
            # Execute and verify
            with pytest.raises(TimeoutError):
                await self.protocol.send_request(
                    method="GET",
                    url="https://example.com/api",
                    timeout=0.1
                )
                
            # Check stream was reset
            self.h2_conn.reset_stream.assert_called_once_with(1)
    
    @pytest.mark.asyncio
    async def test_wait_for_response(self):
        """Test waiting for a complete response."""
        # Setup a stream
        stream = HTTP2Stream(1)
        stream.status_code = 200
        stream.headers = {"content-type": "application/json"}
        stream.data_chunks = [b'{"result":', b'"success"}']
        stream.complete = True
        
        # Execute
        result = await self.protocol._wait_for_response(stream)
        
        # Verify
        assert result["status_code"] == 200
        assert result["headers"] == {"content-type": "application/json"}
        assert result["body_source"] == b'{"result":"success"}'
        # Calculate actual size: body (18 bytes) + headers (30 bytes for content-type)
        assert result["response_size"] == 48
    
    @pytest.mark.asyncio
    async def test_wait_for_response_with_error(self):
        """Test waiting for response with an error."""
        # Setup a stream with an error
        stream = HTTP2Stream(1)
        stream.error = ValueError("Stream error")
        
        # Execute and verify
        with pytest.raises(ValueError) as exc_info:
            await self.protocol._wait_for_response(stream)
            
        assert str(exc_info.value) == "Stream error"
    
    @pytest.mark.asyncio
    async def test_wait_for_response_with_connection_error(self):
        """Test waiting for response with a connection error."""
        # Setup
        stream = HTTP2Stream(1)
        self.protocol._error = ConnectionError("Connection reset")
        
        # Execute and verify
        with pytest.raises(ConnectionError) as exc_info:
            await self.protocol._wait_for_response(stream)
            
        assert str(exc_info.value) == "Connection reset"
    
    @pytest.mark.asyncio
    async def test_reader_loop(self):
        """Test the background reader loop."""
        # Setup - create a real stream for this test
        stream = HTTP2Stream(1)
        self.protocol._streams = {1: stream}
        
        # Setup reader to return data once, then empty (connection closed)
        self.reader.read.side_effect = [b'response data', b'']
        
        # Setup h2 connection to return events
        # Create event instances directly without using constructor parameters
        response_received = Mock(spec=h2.events.ResponseReceived)
        response_received.stream_id = 1
        response_received.headers = [(b':status', b'200'), (b'content-type', b'text/plain')]
        
        data_received = Mock(spec=h2.events.DataReceived)
        data_received.stream_id = 1
        data_received.data = b'hello world'
        data_received.flow_controlled_length = 11
        
        stream_ended = Mock(spec=h2.events.StreamEnded)
        stream_ended.stream_id = 1
        
        events = [response_received, data_received, stream_ended]
        self.h2_conn.receive_data.return_value = events
        
        # Start the reader loop
        task = asyncio.create_task(self.protocol._reader_loop())
        
        try:
            # Give it time to process
            await asyncio.sleep(0.1)
            
            # Check that the stream received the data
            assert stream.status_code == 200
            assert stream.headers["content-type"] == "text/plain"
            assert stream.data_chunks == [b'hello world']
            assert stream.complete
            
            # Check connection state
            assert self.protocol._closed
            assert isinstance(self.protocol._error, ConnectionError)
        finally:
            # Clean up
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    @pytest.mark.asyncio
    async def test_reader_loop_exception(self):
        """Test reader loop with an exception."""
        # Setup - create streams
        stream1 = HTTP2Stream(1)
        stream2 = HTTP2Stream(3)
        self.protocol._streams = {1: stream1, 3: stream2}
        
        # Setup reader to raise an exception
        self.reader.read.side_effect = ConnectionError("Connection reset by peer")
        
        # Start the reader loop
        task = asyncio.create_task(self.protocol._reader_loop())
        
        try:
            # Give it time to process
            await asyncio.sleep(0.1)
            
            # Check that all streams received the error
            assert str(stream1.error) == "Connection reset by peer"
            assert str(stream2.error) == "Connection reset by peer"
            
            # Check connection state
            assert self.protocol._closed
            assert isinstance(self.protocol._error, ConnectionError)
        finally:
            # Clean up
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    def test_process_event_response_received(self):
        """Test processing ResponseReceived event."""
        # Setup
        stream = HTTP2Stream(1)
        self.protocol._streams = {1: stream}
        
        # Create event without using constructor parameters
        event = Mock(spec=h2.events.ResponseReceived)
        event.stream_id = 1
        event.headers = [
            (b':status', b'404'),
            (b'content-type', b'text/plain')
        ]
        
        # Execute
        self.protocol._process_event(event)
        
        # Verify
        assert stream.status_code == 404
        assert stream.headers["content-type"] == "text/plain"
    
    def test_process_event_data_received(self):
        """Test processing DataReceived event."""
        # Setup
        stream = HTTP2Stream(1)
        self.protocol._streams = {1: stream}
        
        # Create event without using constructor parameters
        event = Mock(spec=h2.events.DataReceived)
        event.stream_id = 1
        event.data = b'test data'
        event.flow_controlled_length = 9
        
        # Execute
        self.protocol._process_event(event)
        
        # Verify
        assert stream.data_chunks == [b'test data']
        self.h2_conn.acknowledge_received_data.assert_called_once_with(9, 1)
    
    def test_process_event_stream_ended(self):
        """Test processing StreamEnded event."""
        # Setup
        stream = HTTP2Stream(1)
        self.protocol._streams = {1: stream}
        
        # Create event without using constructor parameters
        event = Mock(spec=h2.events.StreamEnded)
        event.stream_id = 1
        
        # Execute
        self.protocol._process_event(event)
        
        # Verify
        assert stream.complete
    
    def test_process_event_stream_reset(self):
        """Test processing StreamReset event."""
        # Setup
        stream = HTTP2Stream(1)
        self.protocol._streams = {1: stream}
        
        # Create event without using constructor parameters
        event = Mock(spec=h2.events.StreamReset)
        event.stream_id = 1
        event.error_code = h2.errors.ErrorCodes.REFUSED_STREAM
        
        # Execute
        self.protocol._process_event(event)
        
        # Verify
        assert isinstance(stream.error, ConnectionError)
        assert "Stream reset by server" in str(stream.error)
    
    def test_process_event_connection_terminated(self):
        """Test processing ConnectionTerminated event."""
        # Create event without using constructor parameters
        event = Mock(spec=h2.events.ConnectionTerminated)
        event.error_code = h2.errors.ErrorCodes.PROTOCOL_ERROR
        event.last_stream_id = 0
        event.additional_data = None
        
        # Execute
        self.protocol._process_event(event)
        
        # Verify
        assert self.protocol._closed
        assert isinstance(self.protocol._error, ConnectionError)
        assert "Connection terminated by server" in str(self.protocol._error)
    
    def test_available_streams(self):
        """Test available_streams property."""
        # Setup
        self.protocol._max_concurrent_streams = 100
        self.protocol._streams = {1: Mock(), 3: Mock(), 5: Mock()}
        
        # Execute and verify
        assert self.protocol.available_streams == 97
    
    @pytest.mark.asyncio
    async def test_close(self):
        """Test closing the connection."""
        # Setup
        self.protocol._initialized = True
        self.protocol._closed = False
        self.h2_conn.data_to_send.return_value = b'goaway frame'
        
        # Create a task that can be awaited
        async def mock_task():
            pass
            
        # Mock the read task
        self.protocol._read_task = asyncio.create_task(mock_task())
        self.protocol._read_task.done = Mock(return_value=False)  # Make done a method that returns False
        self.protocol._read_task.cancel = Mock()  # Make cancel a regular mock
        
        # Execute
        await self.protocol.close()
        
        # Verify
        assert self.protocol._closed
        self.protocol._read_task.cancel.assert_called_once()
        self.h2_conn.close_connection.assert_called_once()
        self.writer.write.assert_called_once_with(b'goaway frame')
        self.writer.close.assert_called_once()
        self.writer.wait_closed.assert_called_once()
        
        # Test closing again is a no-op
        self.h2_conn.reset_mock()
        self.writer.reset_mock()
        
        await self.protocol.close()
        
        self.h2_conn.close_connection.assert_not_called()
        self.writer.close.assert_not_called()


if __name__ == '__main__':
    pytest.main() 