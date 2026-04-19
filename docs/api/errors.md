# Errors API Reference

All exceptions raised by HyperHTTP inherit from
`hyperhttp.HyperHTTPError`. They're re-exported at the top level — import them
straight from `hyperhttp`:

```python
from hyperhttp import (
    HyperHTTPError,
    TransportError,
    ConnectError,
    TLSError,
    ProtocolError,
    RemoteProtocolError,
    ReadError,
    WriteError,
    DNSError,
    TimeoutException,
    ConnectTimeout,
    ReadTimeout,
    WriteTimeout,
    PoolTimeout,
    PoolClosed,
    HTTPStatusError,
    InvalidURL,
    TooManyRedirects,
    CircuitBreakerOpen,
    StreamError,
    StreamConsumed,
    ResponseClosed,
)
```

## Hierarchy

```
HyperHTTPError
├── TransportError
│   ├── ConnectError          # TCP connect failed
│   │   └── ConnectTimeout    # also TimeoutException
│   ├── TLSError              # TLS handshake / cert validation
│   ├── ProtocolError         # framing / parser violation
│   │   ├── RemoteProtocolError
│   │   └── LocalProtocolError
│   ├── ReadError             # socket read failed / unexpected EOF
│   │   └── ReadTimeout       # also TimeoutException
│   ├── WriteError            # socket write failed
│   │   └── WriteTimeout      # also TimeoutException
│   └── DNSError              # name resolution failed
├── TimeoutException
│   ├── ConnectTimeout        # (also ConnectError)
│   ├── ReadTimeout           # (also ReadError)
│   ├── WriteTimeout          # (also WriteError)
│   └── PoolTimeout           # waited too long for a pool slot
├── HTTPStatusError           # raised by Response.raise_for_status()
├── InvalidURL                # URL couldn't be parsed / is unsuitable
├── TooManyRedirects
├── PoolClosed                # request in flight when Client.aclose() ran
├── CircuitBreakerOpen        # host is currently tripped
└── StreamError
    ├── StreamConsumed        # tried to re-read a consumed body
    └── ResponseClosed        # operation on a closed response
```

## Noteworthy exceptions

### `HTTPStatusError`

Raised by `Response.raise_for_status()` for any `4xx` or `5xx` response. It
carries the response object:

```python
try:
    response = await client.get("https://api.example.com/missing")
    response.raise_for_status()
except hyperhttp.HTTPStatusError as e:
    print(e.response.status_code)
    print(e.response.headers)
    await e.response.aread()
    print(e.response.text)
```

### `CircuitBreakerOpen`

Raised when a host has been marked unhealthy by the circuit breaker.

```python
except hyperhttp.CircuitBreakerOpen as e:
    print(e.host)        # e.g. "api.example.com:443"
    print(e.remaining)   # seconds until the breaker enters HALF-OPEN
```

### `ConnectTimeout` vs. `ReadTimeout`

These are distinct phases. `ConnectTimeout` means the TCP connect exceeded
`connect_timeout`. `ReadTimeout` means the server accepted the connection but
didn't produce a response within the `read` phase of the timeout.

```python
hyperhttp.Client(
    timeout=hyperhttp.Timeout(connect=5.0, read=30.0, write=30.0, pool=2.0),
)
```

### `RetryError`

Not re-exported at the top level — import from
`hyperhttp.errors.retry` if you need to match on it:

```python
from hyperhttp.errors.retry import RetryError

try:
    await client.get("https://flaky.example.com/thing")
except RetryError as e:
    print(e.original_exception)
    print(e.retry_state.attempt_count)
```

`RetryError` is only raised once at least one retry has been attempted. The
**first** failure always surfaces the original typed exception.

## Error classification

Errors are mapped to categories for retry / circuit-breaker decisions. The
defaults:

| Category      | Exceptions                                                       |
|---------------|------------------------------------------------------------------|
| `CONNECTION`  | `ConnectError`, `DNSError`                                       |
| `TIMEOUT`     | `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout`   |
| `TRANSIENT`   | `ReadError`, `WriteError`, `ProtocolError`                       |
| `SERVER`      | `HTTPStatusError` with 5xx status                                |
| `RATE_LIMIT`  | `HTTPStatusError` with 429                                       |
| `FATAL`       | `InvalidURL`, `TooManyRedirects`, 4xx status (other than 429)    |

Pass `retry_categories=[...]` to `RetryPolicy` to change which ones are
retried. See the [Retry Policy](retry.md) page.

## Patterns

### Catch the broad base class

```python
try:
    response = await client.get(url)
    response.raise_for_status()
except hyperhttp.HyperHTTPError as e:
    logger.warning("request failed: %s", e)
```

### Handle timeouts distinctly from other transport failures

```python
try:
    response = await client.get(url)
except hyperhttp.TimeoutException:
    # connect/read/write/pool — any phase
    ...
except hyperhttp.TransportError:
    # TCP, TLS, DNS, framing
    ...
```

### Drain the response before re-raising

If you `raise_for_status()` and want to log the body, read it first:

```python
response = await client.get(url)
if not response.is_success:
    await response.aread()
    logger.error("upstream returned %s: %s",
                 response.status_code, response.text[:500])
    response.raise_for_status()
```
