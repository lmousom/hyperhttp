# Errors API Reference

This page documents the error types and handling in HyperHTTP.

## Exception Hierarchy

```
RequestError
├── HTTPError
│   ├── ClientError
│   └── ServerError
├── ConnectionError
│   ├── ConnectTimeout
│   └── ReadTimeout
├── TimeoutError
├── TooManyRedirects
└── ValidationError
```

## Base Exceptions

### RequestError

```python
class RequestError(Exception):
    """Base exception for all HyperHTTP errors."""
    
    @property
    def request(self) -> Request:
        """The request that caused this error."""
```

### HTTPError

```python
class HTTPError(RequestError):
    """
    Exception for HTTP error responses (4xx-5xx).
    
    Attributes:
        status_code (int): HTTP status code
        reason (str): Status reason phrase
        response (Response): Full response object
    """
```

## HTTP Status Errors

### ClientError

```python
class ClientError(HTTPError):
    """Exception for 4xx client errors."""
```

### ServerError

```python
class ServerError(HTTPError):
    """Exception for 5xx server errors."""
```

## Network Errors

### ConnectionError

```python
class ConnectionError(RequestError):
    """Base class for connection-related errors."""
```

### ConnectTimeout

```python
class ConnectTimeout(ConnectionError):
    """Exception for connection timeout."""
```

### ReadTimeout

```python
class ReadTimeout(ConnectionError):
    """Exception for read timeout."""
```

## Other Errors

### TimeoutError

```python
class TimeoutError(RequestError):
    """Exception for request timeout."""
```

### TooManyRedirects

```python
class TooManyRedirects(RequestError):
    """Exception when max redirects is exceeded."""
```

### ValidationError

```python
class ValidationError(RequestError):
    """Exception for request validation errors."""
```

## Error Categories

Errors are categorized for retry purposes:

```python
ERROR_CATEGORIES = {
    "TRANSIENT": [
        ConnectionError,
        TimeoutError,
        ServerError
    ],
    "TIMEOUT": [
        ConnectTimeout,
        ReadTimeout,
        TimeoutError
    ],
    "SERVER": [
        ServerError
    ],
    "RATE_LIMIT": [
        TooManyRequests  # HTTP 429
    ],
    "CONNECTION": [
        ConnectionError
    ]
}
```

## Error Handling Examples

### Basic Error Handling

```python
from hyperhttp import Client
from hyperhttp.errors import HTTPError, ConnectionError, TimeoutError

async with Client() as client:
    try:
        response = await client.get("https://api.example.com/users")
        response.raise_for_status()
    except HTTPError as e:
        print(f"HTTP {e.status_code}: {e.reason}")
    except ConnectionError as e:
        print(f"Connection failed: {e}")
    except TimeoutError as e:
        print(f"Request timed out: {e}")
```

### Handling Specific Status Codes

```python
from hyperhttp.errors import ClientError, ServerError

try:
    response = await client.get("https://api.example.com/users")
    response.raise_for_status()
except ClientError as e:
    if e.status_code == 404:
        print("Resource not found")
    elif e.status_code == 401:
        print("Authentication required")
    elif e.status_code == 403:
        print("Permission denied")
except ServerError as e:
    print(f"Server error: {e.status_code}")
```

### Custom Error Handling

```python
class CustomError(RequestError):
    """Custom error type for specific handling."""

def handle_response(response):
    if response.status_code == 418:  # I'm a teapot
        raise CustomError("This server is a teapot!")
    response.raise_for_status()

try:
    response = await client.get("https://api.example.com/coffee")
    handle_response(response)
except CustomError as e:
    print(f"Custom error: {e}")
```

### Retry Categories

```python
from hyperhttp.errors.retry import RetryPolicy

# Retry only timeout errors
retry_policy = RetryPolicy(
    max_retries=3,
    retry_categories=["TIMEOUT"]
)

# Retry both timeouts and server errors
retry_policy = RetryPolicy(
    max_retries=3,
    retry_categories=["TIMEOUT", "SERVER"]
)
```

## Best Practices

1. **Always Check Status**: Use `response.raise_for_status()` to catch HTTP errors
2. **Specific to General**: Handle specific exceptions before general ones
3. **Categorize Errors**: Use error categories for retry policies
4. **Custom Handling**: Create custom error types for specific needs
5. **Log Details**: Include error details in logs for debugging
6. **Graceful Degradation**: Provide fallback behavior for errors 