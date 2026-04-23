# Client API Reference

## `hyperhttp.Client`

The main entry point. Reuse a single instance for the lifetime of your
application — it owns the connection pool, DNS cache, retry handler, and
circuit-breaker manager.

```python
class Client:
    def __init__(
        self,
        *,
        base_url: str = "",
        headers: HeadersInput = None,
        cookies: CookiesInput = None,
        timeout: float | Timeout | None = 30.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        keepalive_expiry: float = 120.0,
        http2: bool = True,
        verify: bool | str = True,
        cert: tuple[str, str] | str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        connect_timeout: float | None = 10.0,
        happy_eyeballs_delay: float = 0.25,
        follow_redirects: bool = False,
        max_redirects: int = 20,
        retry: RetryPolicy | bool | None = None,
        circuit_breaker_manager: DomainCircuitBreakerManager | None = None,
        telemetry: ErrorTelemetry | None = None,
        user_agent: str | None = None,
        accept_compressed: bool = True,
        proxies: str | ProxyURL | Mapping[str, str | ProxyURL | None] | None = None,
        trust_env: bool = True,
        auth: Auth | tuple[str, str] | None = None,
        event_hooks: Mapping[str, Iterable[Callable[..., Any]]] | None = None,
        transport: Transport | None = None,
    ) -> None: ...
```

### Key arguments

| Argument                     | Description                                                                 |
|------------------------------|-----------------------------------------------------------------------------|
| `base_url`                   | Prepended to relative URLs passed to `get`/`post`/etc.                      |
| `headers` / `cookies`        | Defaults applied to every request.                                          |
| `timeout`                    | Scalar seconds, or a `Timeout(connect=, read=, write=, pool=)` object.      |
| `max_connections`            | Global cap across all hosts.                                                |
| `max_keepalive_connections`  | Per-host keepalive cap.                                                     |
| `keepalive_expiry`           | Seconds an idle connection is kept around before being closed.              |
| `http2`                      | Negotiate HTTP/2 via ALPN. Falls back to HTTP/1.1 for non-h2 hosts.         |
| `verify`                     | `True` (default), `False`, or a path to a CA bundle.                        |
| `cert`                       | `("cert.pem", "key.pem")` for mutual TLS.                                   |
| `ssl_context`                | Bring your own `ssl.SSLContext`.                                            |
| `connect_timeout`            | TCP connect timeout, independent of request `read` timeout.                 |
| `happy_eyeballs_delay`       | Seconds to wait before racing the second address family.                    |
| `follow_redirects`           | Off by default. Enable globally or pass `follow_redirects=True` per request.|
| `max_redirects`              | Redirect chain cap.                                                         |
| `retry`                      | `RetryPolicy` instance, `True` for defaults, or `False`/`None` to disable.  |
| `circuit_breaker_manager`    | Per-host circuit breaker.                                                   |
| `telemetry`                  | Hook called on every retry attempt.                                         |
| `user_agent`                 | Overrides the default `hyperhttp/<version>` User-Agent.                     |
| `accept_compressed`          | Advertise and transparently decode gzip/deflate (plus br/zstd if installed).|
| `proxies`                    | Proxy URL (string) or per-scheme mapping (`http` / `https` / `all`). See [Proxies](../advanced.md#proxies). |
| `trust_env`                  | When `True` (default), honour `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`. |
| `auth`                       | Default auth for every request. `Auth` instance, `("user", "pass")` tuple (= `BasicAuth`), or `None`. See [Authentication](../advanced.md#authentication). |
| `event_hooks`                | `{"request": [...], "response": [...]}` — sync or async callables invoked per network attempt. See [Event hooks](../advanced.md#event-hooks). |
| `transport`                  | Custom transport instance. When set, bypasses the connection pool entirely (no DNS, no TLS, no sockets). Primary use case is `MockTransport` for tests — see the [Testing guide](../testing.md). |

### Methods

```python
async def get(url, **kwargs) -> Response
async def head(url, **kwargs) -> Response
async def options(url, **kwargs) -> Response
async def delete(url, **kwargs) -> Response
async def post(url, **kwargs) -> Response
async def put(url, **kwargs) -> Response
async def patch(url, **kwargs) -> Response

async def request(
    method: str,
    url: str,
    *,
    params=None,
    headers=None,
    cookies=None,
    content=None,       # bytes, bytearray, memoryview, or async iter[bytes]
    data=None,          # form-encoded dict (or text fields when files= is set)
    files=None,         # multipart file fields — see "File uploads" in advanced docs
    json=...,           # JSON-serialisable object
    timeout=...,        # per-request override
    follow_redirects=...,
    retry: bool = True, # set to False to bypass the client's retry policy
    auth=...,           # per-request auth override; None disables the client default
) -> Response

async def stream(method: str, url: str, **kwargs) -> Response

async def aclose() -> None
def get_pool_stats() -> dict[str, dict[str, int]]
```

Either `await client.aclose()` on shutdown or use the client as an async
context manager:

```python
async with hyperhttp.Client() as client:
    response = await client.get("https://example.com")
```

## `hyperhttp.Response`

Returned by every request method. The body is lazy — stream via
`aiter_bytes`/`aiter_text`/`aiter_lines`, or call `aread()` once to materialize
it.

### Attributes

| Attribute        | Type                     | Notes                                                |
|------------------|--------------------------|------------------------------------------------------|
| `status_code`    | `int`                    | HTTP status.                                         |
| `http_version`   | `str`                    | `"HTTP/1.1"` or `"HTTP/2"`.                          |
| `headers`        | `Headers`                | Case-insensitive multi-dict.                         |
| `url`            | `URL`                    | Final URL, after redirects.                          |
| `request`        | `Request`                | The originating request.                             |
| `elapsed`        | `float`                  | Seconds from request dispatch to headers received.   |
| `is_success`     | `bool`                   | `200 <= status_code < 300`.                          |
| `is_redirect`    | `bool`                   | Status in `(301, 302, 303, 307, 308)`.               |
| `encoding`       | `str`                    | From `Content-Type` charset, else `utf-8`.           |

### Methods

```python
async def aread() -> bytes          # materialize the full body
async def aclose() -> None          # release the underlying connection

async def aiter_raw()    -> AsyncIterator[bytes]   # raw, still-encoded chunks
async def aiter_bytes(chunk_size: int | None = None) -> AsyncIterator[bytes]
async def aiter_text(chunk_size: int | None = None, encoding: str | None = None) -> AsyncIterator[str]
async def aiter_lines(encoding: str | None = None) -> AsyncIterator[str]

def raise_for_status() -> None      # raises HTTPStatusError for 4xx/5xx
def json() -> Any                   # sync, call after aread()
```

Sync properties available **after** `await response.aread()`:

```python
response.content   # bytes
response.text      # str (decoded with response.encoding)
response.json()    # parsed JSON (orjson if installed)
```

### Context manager

```python
async with await client.get("https://example.com/stream") as response:
    async for chunk in response.aiter_bytes():
        ...
# response.aclose() is called on exit
```

## `hyperhttp.Timeout`

```python
hyperhttp.Timeout(
    timeout: float | None = None,  # applied to any unset phase
    *,
    connect: float | None = ...,
    read: float | None = ...,
    write: float | None = ...,
    pool: float | None = ...,
)
```

Passing a plain `float` to `Client(timeout=...)` is equivalent to setting all
four phases to the same value. Any phase set to `None` is disabled.

## Examples

### Per-request overrides

```python
response = await client.get(
    "https://api.example.com/slow",
    timeout=5.0,
    headers={"X-Request-ID": "abc"},
    follow_redirects=True,
)
```

### Shared client with base URL

```python
client = hyperhttp.Client(
    base_url="https://api.example.com",
    headers={"Authorization": "Bearer TOKEN"},
    timeout=30.0,
    max_connections=200,
)

async with client:
    response = await client.get("/users")        # → https://api.example.com/users
    response = await client.post("/users", json={"name": "alice"})
```

### Streaming download

```python
async with hyperhttp.Client() as client:
    async with await client.get("https://example.com/large.bin") as response:
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            sink.write(chunk)
```

### Multipart upload

```python
import pathlib, hyperhttp

async with hyperhttp.Client() as client:
    response = await client.post(
        "https://api.example.com/upload",
        data={"user": "alice", "note": "report"},
        files={
            "avatar": ("me.png", b"\x89PNG...", "image/png"),
            "report": pathlib.Path("./report.pdf"),
        },
    )
```

See [File uploads](../advanced.md#file-uploads) for supported part shapes,
the `MultipartEncoder` / `MultipartFile` advanced API, and benchmarks.
