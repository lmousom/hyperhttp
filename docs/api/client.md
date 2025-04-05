# Client API Reference

This page documents the main `Client` class and its methods.

## Client

```python
class Client:
    """
    The main HTTP client class for making requests.
    
    Args:
        base_url (str, optional): Base URL for all requests
        headers (dict, optional): Default headers for all requests
        params (dict, optional): Default query parameters for all requests
        timeout (float, optional): Default timeout in seconds
        max_connections (int, optional): Maximum number of connections
        max_keepalive_connections (int, optional): Maximum number of keepalive connections
        max_keepalive (float, optional): Keepalive timeout in seconds
        http2_only (bool, optional): Force HTTP/2 for all requests
        enable_http2 (bool, optional): Enable HTTP/2 support
        retry_policy (RetryPolicy, optional): Retry policy for failed requests
        circuit_breaker (CircuitBreaker, optional): Circuit breaker for failure detection
        buffer_pool (BufferPool, optional): Buffer pool for memory optimization
        metrics (MetricsCollector, optional): Metrics collector
        tracer (RequestTracer, optional): Request tracer
        ssl_context (ssl.SSLContext, optional): Custom SSL context
        resolver (CustomResolver, optional): Custom DNS resolver
    """
```

## Making Requests

### GET Request

```python
async def get(
    self,
    url: str,
    *,
    params: dict = None,
    headers: dict = None,
    timeout: float = None,
    **kwargs
) -> Response:
    """
    Send a GET request.
    
    Args:
        url: URL for the request
        params: Query parameters
        headers: Request headers
        timeout: Request timeout in seconds
        **kwargs: Additional arguments
        
    Returns:
        Response: The response object
    """
```

### POST Request

```python
async def post(
    self,
    url: str,
    *,
    data: Any = None,
    json: Any = None,
    params: dict = None,
    headers: dict = None,
    timeout: float = None,
    **kwargs
) -> Response:
    """
    Send a POST request.
    
    Args:
        url: URL for the request
        data: Form data or raw request body
        json: JSON data (will be serialized)
        params: Query parameters
        headers: Request headers
        timeout: Request timeout in seconds
        **kwargs: Additional arguments
        
    Returns:
        Response: The response object
    """
```

### PUT Request

```python
async def put(
    self,
    url: str,
    *,
    data: Any = None,
    json: Any = None,
    params: dict = None,
    headers: dict = None,
    timeout: float = None,
    **kwargs
) -> Response:
    """
    Send a PUT request.
    
    Args:
        url: URL for the request
        data: Form data or raw request body
        json: JSON data (will be serialized)
        params: Query parameters
        headers: Request headers
        timeout: Request timeout in seconds
        **kwargs: Additional arguments
        
    Returns:
        Response: The response object
    """
```

### PATCH Request

```python
async def patch(
    self,
    url: str,
    *,
    data: Any = None,
    json: Any = None,
    params: dict = None,
    headers: dict = None,
    timeout: float = None,
    **kwargs
) -> Response:
    """
    Send a PATCH request.
    
    Args:
        url: URL for the request
        data: Form data or raw request body
        json: JSON data (will be serialized)
        params: Query parameters
        headers: Request headers
        timeout: Request timeout in seconds
        **kwargs: Additional arguments
        
    Returns:
        Response: The response object
    """
```

### DELETE Request

```python
async def delete(
    self,
    url: str,
    *,
    params: dict = None,
    headers: dict = None,
    timeout: float = None,
    **kwargs
) -> Response:
    """
    Send a DELETE request.
    
    Args:
        url: URL for the request
        params: Query parameters
        headers: Request headers
        timeout: Request timeout in seconds
        **kwargs: Additional arguments
        
    Returns:
        Response: The response object
    """
```

## Response Object

```python
class Response:
    """
    HTTP Response object.
    
    Attributes:
        status_code (int): HTTP status code
        reason (str): Status reason phrase
        headers (Headers): Response headers
        cookies (Cookies): Response cookies
        url (str): Final URL after redirects
        history (List[Response]): Redirect history
        elapsed (float): Request duration in seconds
        encoding (str): Response encoding
    """
    
    async def text(self) -> str:
        """Get response content as text."""
        
    async def json(self) -> Any:
        """Get response content as JSON."""
        
    async def read(self) -> bytes:
        """Get raw response content."""
        
    def raise_for_status(self) -> None:
        """Raise an exception for error status codes."""
        
    async def stream(self) -> AsyncIterator[bytes]:
        """Stream response content."""
        
    async def close(self) -> None:
        """Close the response and release resources."""
```

## Context Manager

The `Client` class can be used as an async context manager:

```python
async with Client() as client:
    response = await client.get("https://example.com")
```

## Examples

### Basic GET Request

```python
async with Client() as client:
    response = await client.get("https://api.example.com/users")
    data = await response.json()
```

### POST with JSON

```python
async with Client() as client:
    response = await client.post(
        "https://api.example.com/users",
        json={"name": "John", "email": "john@example.com"}
    )
    result = await response.json()
```

### Streaming Response

```python
async with Client() as client:
    async with client.stream("https://example.com/large-file") as response:
        async for chunk in response.iter_chunks(chunk_size=8192):
            process_chunk(chunk)
```

### Custom Configuration

```python
client = Client(
    base_url="https://api.example.com",
    headers={"Authorization": "Bearer token123"},
    timeout=30.0,
    max_connections=100,
    retry_policy=RetryPolicy(max_retries=3)
) 