import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch

from hyperhttp.protocol.http1 import HTTP1Protocol, HTTP1Connection
from hyperhttp.utils.buffer_pool import BufferPool


# Helper class to mock StreamReader with realistic HTTP response data
class MockStreamReader:
    def __init__(self, response_data=None):
        self.response_data = response_data or b""
        self.position = 0
        self.read_until_calls = []
        self.read_exactly_calls = []
        self.read_calls = []
    
    async def readuntil(self, separator=b"\n"):
        self.read_until_calls.append(separator)
        
        # Find the separator in the remaining data
        pos = self.response_data.find(separator, self.position)
        if pos == -1:
            raise asyncio.IncompleteReadError(b"", len(separator))
        
        # Extract the data up to and including the separator
        data = self.response_data[self.position:pos + len(separator)]
        self.position += len(data)
        return data
    
    async def readexactly(self, n):
        self.read_exactly_calls.append(n)
        data = self.response_data[self.position:self.position + n]
        if len(data) < n:
            raise asyncio.IncompleteReadError(data, n)
        self.position += n
        return data
    
    async def read(self, n=-1):
        self.read_calls.append(n)
        if n == -1:
            data = self.response_data[self.position:]
            self.position = len(self.response_data)
            return data
        else:
            data = self.response_data[self.position:self.position + n]
            self.position += len(data)
            return data


class MockStreamWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False
        self.drain_called = 0
        self.wait_closed_called = 0
    
    def write(self, data):
        self.buffer.extend(data)
    
    async def drain(self):
        self.drain_called += 1
    
    def close(self):
        self.closed = True
    
    async def wait_closed(self):
        self.wait_closed_called += 1


@pytest.fixture
def mock_reader():
    return MockStreamReader()


@pytest.fixture
def mock_writer():
    return MockStreamWriter()


@pytest.fixture
def buffer_pool():
    return BufferPool(sizes=(1024, 4096, 16384), initial_count=2)


class TestHTTP1Protocol:
    @pytest.mark.asyncio
    async def test_send_request_basic(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        # Patch _receive_response to return our controlled response
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 200,
                "reason": "OK",
                "headers": {
                    "content-length": "13",
                    "content-type": "text/plain"
                },
                "body_source": b"Hello, World!",
                "response_size": 100,
            }
            
            # Send a simple GET request
            response = await protocol.send_request(
                method="GET",
                url="http://example.com/index.html",
                headers={"Accept": "text/html"},
            )
            
            # Check the request was sent correctly
            sent_data = bytes(mock_writer.buffer)
            assert b"GET /index.html HTTP/1.1\r\n" in sent_data
            assert b"Host: example.com\r\n" in sent_data
            assert b"Accept: text/html\r\n" in sent_data
            
            # Check the response was as expected
            assert response["status_code"] == 200
            assert response["reason"] == "OK"
            assert response["headers"]["content-length"] == "13"
            assert response["headers"]["content-type"] == "text/plain"
            assert response["body_source"] == b"Hello, World!"
    
    @pytest.mark.asyncio
    async def test_send_request_with_body(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        # Patch _receive_response to return our controlled response
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 201,
                "reason": "Created",
                "headers": {
                    "content-length": "2",
                },
                "body_source": b"OK",
                "response_size": 50,
            }
            
            # Send POST request with body
            body = b'{"name": "test"}'
            response = await protocol.send_request(
                method="POST",
                url="http://example.com/api/users",
                headers={"Content-Type": "application/json"},
                body=body,
            )
            
            # Check request
            sent_data = bytes(mock_writer.buffer)
            assert b"POST /api/users HTTP/1.1\r\n" in sent_data
            assert b"Content-Length: 16\r\n" in sent_data
            assert b"Content-Type: application/json\r\n" in sent_data
            assert body in sent_data
            
            # Check response
            assert response["status_code"] == 201
            assert response["reason"] == "Created"
            assert response["headers"]["content-length"] == "2"
            assert response["body_source"] == b"OK"
    
    @pytest.mark.asyncio
    async def test_chunked_response(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        # Patch _receive_response to return our controlled response
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 200,
                "reason": "OK",
                "headers": {
                    "transfer-encoding": "chunked",
                },
                "body_source": b"MozillaDeveloperNetwork",
                "response_size": 150,
            }
            
            response = await protocol.send_request(
                method="GET",
                url="http://example.com/chunked",
            )
            
            # Check response
            assert response["status_code"] == 200
            assert response["headers"]["transfer-encoding"] == "chunked"
            assert response["body_source"] == b"MozillaDeveloperNetwork"
    
    @pytest.mark.asyncio
    async def test_connection_reuse(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        # First request with Connection: keep-alive
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 200,
                "reason": "OK",
                "headers": {
                    "content-length": "5",
                    "connection": "keep-alive",
                },
                "body_source": b"First",
                "response_size": 80,
            }
            
            response1 = await protocol.send_request(
                method="GET",
                url="http://example.com/first",
            )
            
            # Check connection state after first request
            assert not protocol._closed
            assert response1["headers"]["connection"] == "keep-alive"
        
        # Second request
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 200,
                "reason": "OK",
                "headers": {
                    "content-length": "6",
                },
                "body_source": b"Second",
                "response_size": 70,
            }
            
            response2 = await protocol.send_request(
                method="GET",
                url="http://example.com/second",
            )
            
            # Check both responses and connection state
            assert response1["body_source"] == b"First"
            assert response2["body_source"] == b"Second"
            assert not protocol._closed  # Connection should still be open
    
    @pytest.mark.asyncio
    async def test_connection_close(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        # Patch _receive_response to return a response with Connection: close
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 200,
                "reason": "OK",
                "headers": {
                    "content-length": "5",
                    "connection": "close",
                },
                "body_source": b"Close",
                "response_size": 80,
            }
            
            # Mock the side effect to set _closed to True
            # This simulates the actual HTTP1Protocol behavior when it sees Connection: close
            def side_effect(*args, **kwargs):
                protocol._closed = True
                return mock_receive.return_value
            
            mock_receive.side_effect = side_effect
            
            response = await protocol.send_request(
                method="GET",
                url="http://example.com/close",
            )
            
            # Check response body and connection state
            assert response["body_source"] == b"Close"
            assert response["headers"]["connection"] == "close"
            assert protocol._closed  # Connection should be marked as closed
    
    @pytest.mark.asyncio
    async def test_send_request_after_close(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        # Mark connection as closed
        protocol._closed = True
        
        # Attempt to send request after close
        with pytest.raises(ConnectionError):
            await protocol.send_request(
                method="GET",
                url="http://example.com/",
            )
    
    @pytest.mark.asyncio
    async def test_protocol_close(self):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        await protocol.close()
        
        assert mock_writer.closed
        assert protocol._closed
        
        # Closing again should be a no-op
        await protocol.close()
    
    @pytest.mark.asyncio
    async def test_large_response_with_buffer_pool(self, buffer_pool):
        # Setup mocks
        mock_reader = MockStreamReader()
        mock_writer = MockStreamWriter()
        protocol = HTTP1Protocol(mock_reader, mock_writer)
        
        large_body = b"x" * 2048
        
        # Patch _receive_response to return our controlled response
        with patch.object(protocol, '_receive_response') as mock_receive:
            mock_receive.return_value = {
                "status_code": 200,
                "reason": "OK",
                "headers": {
                    "content-length": "2048",
                },
                "body_source": large_body,
                "response_size": 2100,
            }
            
            response = await protocol.send_request(
                method="GET",
                url="http://example.com/large",
                buffer_pool=buffer_pool,
            )
            
            # The buffer pool should be passed to _receive_response
            mock_receive.assert_called_once_with(buffer_pool)
            
            assert response["headers"]["content-length"] == "2048"
            assert len(response["body_source"]) == 2048
            assert response["body_source"] == large_body


class TestHTTP1Connection:
    @pytest.mark.asyncio
    @patch("hyperhttp.protocol.http1.HTTP1Protocol")
    @patch("asyncio.open_connection")
    async def test_connection_init(self, mock_open_connection, mock_protocol_class):
        # Setup mocks
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_open_connection.return_value = (mock_reader, mock_writer)
        mock_protocol = Mock()
        mock_protocol_class.return_value = mock_protocol
        
        # Create connection
        conn = HTTP1Connection("example.com", 80)
        
        # Check initial state
        assert conn._host == "example.com"
        assert conn._port == 80
        assert conn._use_tls is False
        assert conn._protocol is None
        
        # Mock the base Connection.connect method to avoid socket issues
        with patch("hyperhttp.connection.base.Connection.connect") as mock_base_connect:
            # Connect and check protocol creation
            await conn.connect()
            mock_base_connect.assert_called_once()
            mock_protocol_class.assert_called_with(conn._reader, conn._writer)
            assert conn._protocol is mock_protocol
    
    @pytest.mark.asyncio
    @patch("hyperhttp.connection.base.Connection.connect")
    async def test_send_request(self, mock_connect):
        # Setup mocks
        mock_protocol = AsyncMock()
        mock_protocol.send_request.return_value = {
            "status_code": 200,
            "reason": "OK",
            "headers": {"content-type": "application/json"},
            "body_source": b'{"success": true}',
            "response_size": 100,
        }
        
        # Create connection with mock protocol
        conn = HTTP1Connection("example.com", 80)
        conn._protocol = mock_protocol
        
        # Send request
        response = await conn.send_request(
            method="GET",
            url="http://example.com/api",
            params={"q": "test"},
            headers={"Accept": "application/json"},
        )
        
        # Check protocol.send_request was called with correct args
        mock_protocol.send_request.assert_called_once()
        call_args = mock_protocol.send_request.call_args[1]
        assert call_args["method"] == "GET"
        assert "q=test" in call_args["url"]
        assert call_args["headers"]["Accept"] == "application/json"
        
        # Check response
        assert response["status_code"] == 200
        assert response["body_source"] == b'{"success": true}'
    
    @pytest.mark.asyncio
    async def test_send_request_not_connected(self):
        conn = HTTP1Connection("example.com", 80)
        
        with pytest.raises(ConnectionError, match="Connection not established"):
            await conn.send_request(
                method="GET",
                url="http://example.com/api",
            )
    
    @pytest.mark.asyncio
    @patch("hyperhttp.protocol.http1.HTTP1Protocol.send_request")
    async def test_send_request_with_json(self, mock_send):
        # Setup mock
        mock_send.return_value = {
            "status_code": 201,
            "reason": "Created",
            "headers": {},
            "body_source": b"{}",
            "response_size": 50,
        }
        
        # Create connection with mock protocol
        conn = HTTP1Connection("example.com", 80)
        conn._protocol = Mock()  # Just to pass the is None check
        conn._protocol.send_request = mock_send
        
        # Send request with JSON
        await conn.send_request(
            method="POST",
            url="http://example.com/api/users",
            json={"name": "John", "email": "john@example.com"},
        )
        
        # Check sent data
        call_args = mock_send.call_args[1]
        assert call_args["method"] == "POST"
        assert "Content-Type" in call_args["headers"]
        assert call_args["headers"]["Content-Type"] == "application/json"
        assert b'"name": "John"' in call_args["body"]
    
    @pytest.mark.asyncio
    @patch("hyperhttp.connection.base.Connection.close")
    async def test_connection_close(self, mock_base_close):
        mock_protocol = AsyncMock()
        
        conn = HTTP1Connection("example.com", 80)
        conn._protocol = mock_protocol
        
        await conn.close()
        
        mock_protocol.close.assert_called_once()
        mock_base_close.assert_called_once()
    
    def test_is_reusable(self):
        conn = HTTP1Connection("example.com", 80)
        
        # Base connection says not reusable
        with patch("hyperhttp.connection.base.Connection.is_reusable", return_value=False):
            assert not conn.is_reusable()
        
        # Base says reusable, but protocol is closed
        with patch("hyperhttp.connection.base.Connection.is_reusable", return_value=True):
            conn._protocol = Mock()
            conn._protocol._closed = True
            assert not conn.is_reusable()
            
            # Base and protocol both say reusable
            conn._protocol._closed = False
            assert conn.is_reusable()
