# Advanced Features

Start with [Basic Usage](usage.md) before diving in here.

## HTTP/2

HTTP/2 is negotiated via ALPN during the TLS handshake and is **on by default**.

```python
client = hyperhttp.Client(http2=True)   # default
client = hyperhttp.Client(http2=False)  # force HTTP/1.1 only
```

When a host speaks h2, concurrent requests to that host share a single TCP
connection and multiplex as independent streams (bounded by the server's
`MAX_CONCURRENT_STREAMS`). The client transparently falls back to HTTP/1.1
for hosts that don't advertise `h2` in ALPN.

```python
async with hyperhttp.Client() as client:
    responses = await asyncio.gather(
        client.get("https://example.com/api/1"),
        client.get("https://example.com/api/2"),
        client.get("https://example.com/api/3"),
    )
```

## Connection pooling

```python
client = hyperhttp.Client(
    max_connections=200,           # global cap across all hosts
    max_keepalive_connections=32,  # per-host keepalive cap
    keepalive_expiry=120.0,        # seconds an idle connection is kept around
)
```

The pool enforces both the global and per-host cap with FIFO waiter fairness:
the oldest request waiting for a connection is served first.

You can inspect the pool at runtime:

```python
stats = client.get_pool_stats()
# {"example.com:443": {"idle": 4, "in_use": 2, "total": 6}, ...}
```

## Retry policy

Retries are opt-in. Pass a `RetryPolicy` to `Client(retry=...)`:

```python
from hyperhttp import Client
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import ExponentialBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"],
    status_force_list=[429, 500, 502, 503, 504],
    backoff_strategy=ExponentialBackoff(
        base=0.1,         # initial delay (seconds)
        factor=2.0,       # multiplier per attempt
        max_backoff=30.0, # cap on any single wait
        jitter=True,
    ),
    respect_retry_after=True,
)

async with Client(retry=retry_policy) as client:
    response = await client.get("https://api.example.com/things")
```

Decorrelated jitter usually behaves better than classic exponential backoff
under load:

```python
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    backoff_strategy=DecorrelatedJitterBackoff(base=0.1, max_backoff=10.0),
)
```

The **first** failure always surfaces the original typed exception — e.g.
`ReadTimeout`, `ConnectError`. `RetryError` is only raised once at least one
retry has actually been attempted, with the underlying exception available on
`.original_exception`.

Retries can also be disabled per request:

```python
await client.post(url, json=payload, retry=False)
```

## Circuit breaker

When a host keeps failing, the circuit breaker opens and fails fast instead of
piling more timeouts on a sick server:

```python
from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager

cb = DomainCircuitBreakerManager(
    failure_threshold=5,     # consecutive failures before opening
    recovery_timeout=30.0,   # seconds before allowing a probe request
    success_threshold=2,     # consecutive successes to fully close again
)

async with Client(circuit_breaker_manager=cb) as client:
    try:
        response = await client.get("https://api.example.com/things")
    except hyperhttp.CircuitBreakerOpen as e:
        print(f"{e.host} unhealthy for another {e.remaining:.1f}s")
```

Circuit breakers are tracked per host-port. Only the error categories listed in
`DomainCircuitBreakerManager` (by default `CONNECTION`, `TIMEOUT`, `SERVER`,
and `TRANSIENT`) count toward the failure threshold.

## Telemetry

Hook into every retry attempt for metrics/logging:

```python
from hyperhttp.errors.telemetry import ErrorTelemetry

class MyTelemetry(ErrorTelemetry):
    def record_attempt(self, retry_state, outcome):
        # outcome is "success" | "retry" | "fail"
        ...

async with Client(telemetry=MyTelemetry()) as client:
    ...
```

## TLS

```python
import ssl

ctx = ssl.create_default_context()
ctx.load_cert_chain("client-cert.pem", "client-key.pem")

client = hyperhttp.Client(ssl_context=ctx)
```

Quick toggles:

```python
hyperhttp.Client(verify=False)                       # skip verification (dev only)
hyperhttp.Client(verify="/path/to/ca-bundle.pem")    # custom CA bundle
hyperhttp.Client(cert=("client.pem", "client.key"))  # mutual TLS
```

## DNS and Happy Eyeballs

DNS results are cached with bounded TTL, and dual-stack hosts race IPv6 vs.
IPv4 with a configurable stagger:

```python
client = hyperhttp.Client(
    happy_eyeballs_delay=0.25,  # seconds to wait before racing the other family
    connect_timeout=10.0,
)
```

Set `happy_eyeballs_delay` to `0` to race both families immediately; set it
high to effectively prefer IPv6.

## Custom user agent

```python
client = hyperhttp.Client(user_agent="my-service/1.2.3")
```

Defaults to `hyperhttp/<version>`.

## Next

- [Performance Tips](performance.md)
- [API Reference](api/client.md)
- [Retry Policy](api/retry.md)
- [Errors](api/errors.md)
