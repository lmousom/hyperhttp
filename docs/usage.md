# Basic Usage

This guide covers the common API surface. See [Advanced Features](advanced.md)
for retries, circuit breakers, and HTTP/2 tuning.

## Making requests

### GET

```python
async with hyperhttp.Client() as client:
    response = await client.get("https://api.example.com/users")

    response = await client.get(
        "https://api.example.com/search",
        params={"q": "python", "sort": "stars"},
    )

    response = await client.get(
        "https://api.example.com/protected",
        headers={"Authorization": "Bearer token123"},
    )
```

### POST / PUT / PATCH

```python
async with hyperhttp.Client() as client:
    # JSON body (also sets Content-Type: application/json)
    response = await client.post(
        "https://api.example.com/users",
        json={"name": "John", "email": "john@example.com"},
    )

    # Form-encoded body (Content-Type: application/x-www-form-urlencoded)
    response = await client.post(
        "https://api.example.com/login",
        data={"user": "alice", "password": "secret"},
    )

    # Raw bytes
    response = await client.put(
        "https://api.example.com/blob",
        content=b"...binary...",
    )

    # Async generator for streaming uploads
    async def chunks():
        for i in range(100):
            yield f"chunk-{i}\n".encode()

    response = await client.post(
        "https://api.example.com/upload",
        content=chunks(),
    )
```

### Other methods

```python
await client.head("https://api.example.com/users/1")
await client.options("https://api.example.com/users")
await client.delete("https://api.example.com/users/1")
```

Or call `client.request(method, url, ...)` for arbitrary methods.

## Working with responses

The body is lazy. Call `await response.aread()` once to materialize it, or
iterate the response for streaming access.

```python
async with hyperhttp.Client() as client:
    response = await client.get("https://api.example.com/data")

    await response.aread()
    data_json  = response.json()      # sync after aread()
    data_text  = response.text         # sync property after aread()
    data_bytes = response.content      # sync property after aread()
```

### Response attributes

```python
print(response.status_code)    # 200
print(response.http_version)   # "HTTP/1.1" or "HTTP/2"
print(response.url)            # final URL (after redirects)
print(response.headers["content-type"])
print(response.elapsed)        # seconds from request to response
print(response.is_success)     # 2xx
print(response.is_redirect)    # 30x
```

### Raising on HTTP errors

```python
response = await client.get("https://api.example.com/missing")
response.raise_for_status()  # raises HTTPStatusError for 4xx/5xx
```

### Streaming the body

```python
async with hyperhttp.Client() as client:
    response = await client.get("https://example.com/large.bin")

    async for chunk in response.aiter_bytes():
        handle_bytes(chunk)

    # Or: iterate text / lines
    async for chunk in response.aiter_text():
        ...

    async for line in response.aiter_lines():
        ...
```

Iterating a response fully consumes it; the connection is returned to the
pool automatically. If you break out early, use `await response.aclose()`
to release the connection.

Responses also support `async with` for deterministic cleanup:

```python
async with await client.get("https://example.com/stream") as response:
    async for chunk in response.aiter_bytes():
        ...
```

## Client configuration

```python
client = hyperhttp.Client(
    base_url="https://api.example.com",
    headers={"User-Agent": "my-app/1.0"},
    cookies={"session": "abc"},
    timeout=30.0,
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=120.0,
    http2=True,
    follow_redirects=False,
    max_redirects=20,
    verify=True,
)
```

Headers and cookies set on the client apply to every request; per-request
`headers=` and `cookies=` merge on top.

## Timeouts

The simplest case is a single scalar:

```python
client = hyperhttp.Client(timeout=10.0)             # applied to all phases
response = await client.get(url, timeout=5.0)       # per-request override
```

For fine-grained control over each phase, pass a `Timeout`:

```python
from hyperhttp import Timeout

client = hyperhttp.Client(
    timeout=Timeout(connect=5.0, read=30.0, write=30.0, pool=2.0),
)
```

Set any phase to `None` to disable it.

## Redirects

Redirects are **off** by default so you can decide how to handle them.

```python
# Enable globally
client = hyperhttp.Client(follow_redirects=True, max_redirects=10)

# ...or per request
response = await client.get(url, follow_redirects=True)
```

`response.url` always reflects the final URL after any redirects.

## Cookies

```python
client = hyperhttp.Client(cookies={"session": "abc"})
response = await client.post("https://example.com/login", data={...})
# Set-Cookie values from the response are persisted on the client
# and sent with subsequent requests to matching hosts.
response = await client.get("https://example.com/profile")
```

## Error handling

All exceptions inherit from `hyperhttp.HyperHTTPError`:

```python
import hyperhttp
from hyperhttp import (
    HyperHTTPError,
    TransportError,
    ConnectError,
    TLSError,
    ReadTimeout,
    ConnectTimeout,
    HTTPStatusError,
    TooManyRedirects,
)

async with hyperhttp.Client() as client:
    try:
        response = await client.get("https://api.example.com/data")
        response.raise_for_status()
    except HTTPStatusError as e:
        print(f"HTTP {e.response.status_code} from {e.response.url}")
    except ConnectTimeout:
        print("Couldn't open a TCP connection in time")
    except ReadTimeout:
        print("Server didn't respond in time")
    except TLSError as e:
        print(f"TLS handshake failed: {e}")
    except TransportError as e:
        print(f"Transport-level failure: {e}")
    except HyperHTTPError as e:
        print(f"Something else went wrong: {e}")
```

See the full hierarchy in [Errors API Reference](api/errors.md).

## Next

- [Advanced Features](advanced.md) — retry, circuit breaker, HTTP/2, TLS
- [Performance Tips](performance.md)
- [API Reference](api/client.md)
