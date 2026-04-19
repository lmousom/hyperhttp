# Performance

## Benchmarks

Measured against `aiohttp` and `httpx` on a local `aiohttp` loopback server
(no network, no DNS, no TLS), 2 000 requests per (client, body size),
concurrency 64. Python 3.12, macOS arm64, uvloop enabled.

| Body size | Client    |     RPS | P50 (ms) | P95 (ms) | P99 (ms) |
|----------:|-----------|--------:|---------:|---------:|---------:|
|    200 B  | hyperhttp | 3 699   |    11.1  |    12.7  |    31.3  |
|    200 B  | aiohttp   | 3 904   |    11.5  |    14.7  |    28.5  |
|    200 B  | httpx     |   199   |   210.4  |   948.9  |  1452.9  |
|   10 KiB  | hyperhttp | 3 527   |    11.8  |    13.7  |    32.2  |
|   10 KiB  | aiohttp   | 3 794   |    11.9  |    15.1  |    29.1  |
|   10 KiB  | httpx     |   203   |   204.8  |   902.1  |  1413.2  |
|    1 MiB  | hyperhttp | 1 303   |    42.7  |    46.5  |    71.2  |
|    1 MiB  | aiohttp   | 1 317   |    43.0  |    47.6  |    61.5  |
|    1 MiB  | httpx     |    98   |   345.1  |  1465.9  |  2784.2  |

HyperHTTP runs within ~5% of `aiohttp` across every body size and is 15–20×
faster than `httpx` on this workload. Loopback numbers reflect client-side CPU
cost; real networks flatten the differences.

Run it yourself:

```bash
pip install 'hyperhttp[bench]'
python examples/benchmark_local.py
```

## Tips for squeezing more out of it

### 1. Install the speed extras

```bash
pip install 'hyperhttp[speed]'
```

This pulls in `uvloop`, `orjson`, `h11`, `brotli`, and `zstandard`. Each is
auto-detected at import time.

### 2. Turn on uvloop

```python
import hyperhttp
hyperhttp.install_uvloop()  # call once at program start
```

This is a no-op if `uvloop` isn't installed (e.g. on Windows).

### 3. Reuse a single `Client`

The `Client` owns the connection pool, DNS cache, and retry handler.
Creating one per request throws all of that away.

```python
# App startup
client = hyperhttp.Client()

# Per-request
response = await client.get(url)

# App shutdown
await client.aclose()
```

### 4. Size the pool for your concurrency

```python
client = hyperhttp.Client(
    max_connections=200,
    max_keepalive_connections=32,
    keepalive_expiry=120.0,
)
```

A good default: `max_connections >= peak_concurrency`,
`max_keepalive_connections >= peak_concurrency_per_host`.

### 5. Let HTTP/2 do the multiplexing

When a host supports HTTP/2 (most public APIs, CDNs, and load balancers do),
every concurrent request reuses a single TCP connection instead of burning
pool slots. Leave `http2=True` (the default).

### 6. Stream large bodies instead of `aread()`-ing them

```python
async with await client.get(url) as response:
    async for chunk in response.aiter_bytes():
        process(chunk)
```

`aread()` is optimised (zero-copy single-chunk fast path; `b"".join` for
multi-chunk), but streaming avoids holding a full copy of the body in memory,
which matters for large files and high concurrency.

### 7. Run concurrently

```python
responses = await asyncio.gather(*(client.get(u) for u in urls))
```

With `asyncio.gather` (or a bounded-concurrency semaphore for larger batches),
HyperHTTP will saturate the pool and multiplex HTTP/2 streams where possible.

### 8. Use decorrelated-jitter backoff if you retry

```python
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff

retry_policy = RetryPolicy(
    max_retries=3,
    backoff_strategy=DecorrelatedJitterBackoff(base=0.1, max_backoff=10.0),
)
```

This spreads retries across clients and avoids thundering-herd pile-ups.

### 9. Put a circuit breaker in front of flaky dependencies

```python
from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager

client = hyperhttp.Client(
    circuit_breaker_manager=DomainCircuitBreakerManager(
        failure_threshold=5,
        recovery_timeout=30.0,
    ),
)
```

When the breaker trips, calls fail fast with `CircuitBreakerOpen` instead of
piling more work onto a sick server.

## Checklist

- [ ] `pip install 'hyperhttp[speed]'`
- [ ] `hyperhttp.install_uvloop()` at startup
- [ ] One `Client` for the app lifetime
- [ ] Pool sized for peak concurrency
- [ ] `http2=True` (the default) for HTTP/2-capable hosts
- [ ] `response.aiter_bytes()` for large downloads
- [ ] `asyncio.gather` or a semaphore for parallelism
- [ ] Retry + circuit breaker for any unreliable upstream
