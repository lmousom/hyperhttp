import pytest
from hyperhttp.client import Client, Response, HttpError

@pytest.fixture
def mock_response_data():
    return {
        "status_code": 200,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body_source": b'{"message": "Hello, World!"}',
        "url": "https://api.example.com/test",
        "request_time": 0.1,
        "request_size": 100,
        "response_size": 200
    }

@pytest.fixture
def response(mock_response_data):
    return Response(**mock_response_data)

@pytest.fixture
async def client():
    client = Client(base_url="https://api.example.com")
    yield client
    await client.close()

class TestResponse:
    @pytest.mark.asyncio
    async def test_body_returns_bytes(self, response):
        body = await response.body()
        assert isinstance(body, bytes)
        assert body == b'{"message": "Hello, World!"}'

    @pytest.mark.asyncio
    async def test_text_returns_decoded_string(self, response):
        text = await response.text()
        assert isinstance(text, str)
        assert text == '{"message": "Hello, World!"}'

    @pytest.mark.asyncio
    async def test_json_returns_parsed_data(self, response):
        data = await response.json()
        assert isinstance(data, dict)
        assert data == {"message": "Hello, World!"}

    def test_get_encoding_default_utf8(self):
        response = Response(200, {}, b"", "https://example.com", 0.1)
        assert response._get_encoding() == "utf-8"

    def test_get_encoding_from_header(self):
        response = Response(
            200,
            {"content-type": "text/plain; charset=iso-8859-1"},
            b"",
            "https://example.com",
            0.1
        )
        assert response._get_encoding() == "iso-8859-1"

    def test_raise_for_status_success(self, response):
        response.raise_for_status()  # Should not raise

    def test_raise_for_status_error(self):
        error_response = Response(404, {}, b"", "https://example.com", 0.1)
        with pytest.raises(HttpError):
            error_response.raise_for_status()

class TestClient:
    @pytest.mark.asyncio
    async def test_client_initialization(self, client):
        assert client.base_url == "https://api.example.com"
        assert isinstance(client.default_headers, dict)
        assert client.default_timeout == 30.0

    @pytest.mark.asyncio
    async def test_get_request(self, client, monkeypatch):
        mock_response = Response(
            200,
            {"content-type": "application/json"},
            b'{"result": "success"}',
            "https://api.example.com/test",
            0.1
        )
        
        async def mock_request(method, url, **kwargs):
            assert method == "GET"
            assert url == "/test"
            assert kwargs["params"] == {"key": "value"}
            return mock_response
            
        monkeypatch.setattr(client, "request", mock_request)

        response = await client.get("/test", params={"key": "value"})
        assert response.status_code == 200
        data = await response.json()
        assert data["result"] == "success"

    @pytest.mark.asyncio
    async def test_post_request_json(self, client, monkeypatch):
        mock_response = Response(
            201,
            {"content-type": "application/json"},
            b'{"id": 1}',
            "https://api.example.com/create",
            0.1
        )
        
        async def mock_request(method, url, **kwargs):
            assert method == "POST"
            assert url == "/create"
            assert kwargs["json"] == {"name": "test"}
            return mock_response
            
        monkeypatch.setattr(client, "request", mock_request)

        payload = {"name": "test"}
        response = await client.post("/create", json=payload)
        assert response.status_code == 201
        data = await response.json()
        assert data["id"] == 1

    @pytest.mark.asyncio
    async def test_error_handling(self, client, monkeypatch):
        async def mock_request(method, url, **kwargs):
            raise HttpError("Not Found")
            
        monkeypatch.setattr(client, "request", mock_request)

        with pytest.raises(HttpError) as exc_info:
            await client.get("/nonexistent")
        assert "Not Found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with Client() as client:
            assert not client._pool_manager._host_pools
        # After exiting context manager, pool manager should be closed
        assert not client._pool_manager._host_pools

    @pytest.mark.parametrize("method", ["put", "patch", "delete", "head", "options"])
    @pytest.mark.asyncio
    async def test_http_methods(self, client, monkeypatch, method):
        mock_response = Response(
            200,
            {"content-type": "application/json"},
            b'{}',
            f"https://api.example.com/test",
            0.1
        )
        
        async def mock_request(method_name, url, **kwargs):
            assert method_name == method.upper()
            assert url == "/test"
            return mock_response
            
        monkeypatch.setattr(client, "request", mock_request)

        method_func = getattr(client, method)
        response = await method_func("/test")
        assert response.status_code == 200
