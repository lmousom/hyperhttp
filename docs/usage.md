# Basic Usage

This guide covers the common use cases and features of HyperHTTP. For more advanced scenarios, check out the [Advanced Features](advanced.md) guide.

## Making Requests

### GET Requests

```python
async with Client() as client:
    # Simple GET
    response = await client.get("https://api.example.com/users")
    
    # GET with query parameters
    response = await client.get(
        "https://api.example.com/search",
        params={"q": "python", "sort": "stars"}
    )
    
    # GET with headers
    response = await client.get(
        "https://api.example.com/protected",
        headers={"Authorization": "Bearer token123"}
    )
```

### POST Requests

```python
async with Client() as client:
    # POST with JSON data
    response = await client.post(
        "https://api.example.com/users",
        json={"name": "John", "email": "john@example.com"}
    )
    
    # POST with form data
    response = await client.post(
        "https://api.example.com/upload",
        data={"key": "value"},
        files={"file": open("image.png", "rb")}
    )
```

### Other HTTP Methods

```python
async with Client() as client:
    # PUT request
    response = await client.put(
        "https://api.example.com/users/1",
        json={"name": "Updated Name"}
    )
    
    # PATCH request
    response = await client.patch(
        "https://api.example.com/users/1",
        json={"email": "newemail@example.com"}
    )
    
    # DELETE request
    response = await client.delete("https://api.example.com/users/1")
```

## Working with Responses

### Response Content

```python
async with Client() as client:
    response = await client.get("https://api.example.com/data")
    
    # Get JSON response
    data = await response.json()
    
    # Get text response
    text = await response.text()
    
    # Get raw bytes
    bytes_data = await response.read()
```

### Response Properties

```python
# Status code and reason
print(response.status_code)  # e.g., 200
print(response.reason)       # e.g., "OK"

# Headers
print(response.headers["content-type"])

# Cookies
print(response.cookies["session"])

# URL after redirects
print(response.url)
```

## Session Management

### Using Sessions

```python
async with Client() as client:
    # Set default headers for all requests
    client.headers.update({
        "User-Agent": "HyperHTTP/1.0",
        "Accept": "application/json"
    })
    
    # Set default parameters
    client.params.update({
        "api_key": "your-api-key"
    })
    
    # All requests will include the headers and parameters
    response = await client.get("https://api.example.com/data")
```

### Cookie Persistence

```python
async with Client() as client:
    # Login request - sets cookies
    await client.post(
        "https://example.com/login",
        data={"username": "user", "password": "pass"}
    )
    
    # Subsequent requests use the same cookies
    response = await client.get("https://example.com/protected")
```

## Timeouts and Retries

### Setting Timeouts

```python
# Set timeout for a specific request
response = await client.get(
    "https://api.example.com/slow",
    timeout=5.0  # 5 seconds
)

# Set default timeout for all requests
client = Client(timeout=10.0)
```

### Automatic Retries

```python
from hyperhttp.errors.retry import RetryPolicy

# Create a retry policy
retry_policy = RetryPolicy(
    max_retries=3,
    retry_categories=["TRANSIENT", "TIMEOUT"]
)

# Use the retry policy
client = Client(retry_policy=retry_policy)
```

## Error Handling

```python
from hyperhttp.errors import (
    HTTPError,
    ConnectionError,
    TimeoutError,
    RequestError
)

async with Client() as client:
    try:
        response = await client.get("https://api.example.com/data")
        response.raise_for_status()
    except HTTPError as e:
        print(f"HTTP error occurred: {e}")
    except ConnectionError as e:
        print(f"Connection error: {e}")
    except TimeoutError as e:
        print(f"Request timed out: {e}")
    except RequestError as e:
        print(f"Request failed: {e}")
```

## Next Steps

- Learn about [Advanced Features](advanced.md) like connection pooling and HTTP/2
- Check out [Performance Tips](performance.md) for optimizing your applications
- Explore the [API Reference](api/client.md) for detailed documentation
