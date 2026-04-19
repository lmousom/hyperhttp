# HyperHTTP

[![PyPI version](https://badge.fury.io/py/hyperhttp.svg)](https://badge.fury.io/py/hyperhttp)
[![Python Versions](https://img.shields.io/pypi/pyversions/hyperhttp.svg)](https://pypi.org/project/hyperhttp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Documentation Status](https://readthedocs.org/projects/hyperhttp/badge/?version=latest)](https://hyperhttp.readthedocs.io/en/latest/?badge=latest)
[![Tests](https://github.com/lmousom/hyperhttp/actions/workflows/tests.yml/badge.svg)](https://github.com/lmousom/hyperhttp/actions/workflows/tests.yml)

A fast, correct async HTTP client for Python. HTTP/1.1 and HTTP/2, built on `asyncio`.

HyperHTTP is designed for services that make a lot of outbound HTTP calls — API gateways, crawlers, load generators, backend-to-backend traffic — where throughput, tail latency, and memory behaviour under concurrency all matter.

## Features

- **HTTP/1.1 and HTTP/2.** HTTP/2 is negotiated via ALPN automatically. Multiple concurrent requests to the same host multiplex as streams over a single TCP connection.
- **Strict, smuggling-resistant parser.** Rejects `Content-Length` + `Transfer-Encoding` conflicts and differing duplicate `Content-Length` headers.
- **Connection pool with global and per-host caps**, FIFO waiter fairness, and connection recycling.
- **Transparent decoding** of `gzip` and `deflate` out of the box; `br` and `zstd` when the optional libraries are installed.
- **DNS cache with Happy Eyeballs v2** — IPv6/IPv4 races with a 250 ms stagger, bounded-TTL cache.
- **Retry and circuit breaker** with error classification, decorrelated-jitter backoff, and `Retry-After` support. The first failure surfaces the original typed exception; `RetryError` only appears after at least one retry has run.
- **Cookies, redirects, streaming bodies, JSON via `orjson` when available.**

## Installation

```bash
pip install hyperhttp

# Optional fast extras: uvloop, orjson, brotli, zstandard, h11
pip install 'hyperhttp[speed]'
```

## Quick start

```python
import asyncio
import hyperhttp

async def main():
    async with hyperhttp.Client() as client:
        response = await client.get("https://example.com")
        await response.aread()
        print(response.status_code, response.text[:120])

        response = await client.post(
            "https://httpbin.org/post",
            json={"key": "value"},
        )
        await response.aread()
        print(response.json())

asyncio.run(main())
```

### Streaming responses

```python
async with hyperhttp.Client() as client:
    response = await client.get("https://example.com/large.bin")
    async for chunk in response.aiter_bytes():
        handle(chunk)
```

### Parallel requests

```python
async with hyperhttp.Client() as client:
    urls = [
        "https://httpbin.org/get",
        "https://httpbin.org/ip",
        "https://httpbin.org/headers",
    ]
    responses = await asyncio.gather(*(client.get(u) for u in urls))
    for r in responses:
        await r.aread()
```

### Retry and circuit breaker

```python
from hyperhttp import Client
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"],
    status_force_list=[429, 500, 502, 503, 504],
    backoff_strategy=DecorrelatedJitterBackoff(base=0.1, max_backoff=10.0),
    respect_retry_after=True,
)

async with Client(retry=retry_policy) as client:
    response = await client.get("https://api.example.com/things")
```

The first attempt is never wrapped in `RetryError` — callers always see the typed transport or timeout exception on the initial failure. `RetryError` is only raised once at least one retry has been attempted.

### Connection pooling

```python
client = Client(
    max_connections=200,           # global cap across all hosts
    max_keepalive_connections=32,  # per host
    http2=True,                    # negotiate HTTP/2 via ALPN
)
```

### HTTP/2

HTTP/2 is negotiated during the TLS handshake. When a host speaks h2, concurrent requests to that host share a single TCP connection and multiplex as streams, bounded by the server-advertised `MAX_CONCURRENT_STREAMS`. Pass `http2=False` to force HTTP/1.1.

### uvloop

```python
import hyperhttp
hyperhttp.install_uvloop()  # no-op if uvloop isn't installed
```

## Benchmarks

Measured against `aiohttp` and `httpx` on a local `aiohttp` loopback server (no network, no DNS, no TLS), 2 000 requests per (client, body size), concurrency 64, Python 3.12, macOS arm64.

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

HyperHTTP runs within ~5% of `aiohttp` across every body size and is 15–20× faster than `httpx` on this workload. Numbers come from loopback, so they reflect client-side CPU cost; real networks flatten the differences.

Run it yourself:

```bash
pip install 'hyperhttp[bench]'
python examples/benchmark_local.py
```

## Documentation

Full documentation: [hyperhttp.readthedocs.io](https://hyperhttp.readthedocs.io/)

## Contributing

Issues and pull requests are welcome. See the [Contributing Guide](https://github.com/lmousom/hyperhttp/blob/main/docs/contributing.md).

## License

MIT — see [LICENSE](https://github.com/lmousom/hyperhttp/blob/main/LICENSE).
