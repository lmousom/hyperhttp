# HyperHTTP

A fast, correct async HTTP client for Python. HTTP/1.1 and HTTP/2, built on
`asyncio`.

HyperHTTP is designed for services that make a lot of outbound HTTP calls —
API gateways, crawlers, load generators, backend-to-backend traffic — where
throughput, tail latency, and memory behaviour under concurrency all matter.

## Features

- **HTTP/1.1 and HTTP/2.** HTTP/2 is negotiated via ALPN automatically.
  Multiple concurrent requests to the same host multiplex as streams over a
  single TCP connection.
- **Strict, smuggling-resistant parser.** Rejects `Content-Length` +
  `Transfer-Encoding` conflicts and differing duplicate `Content-Length`
  headers.
- **Connection pool** with global and per-host caps, FIFO waiter fairness, and
  connection recycling.
- **Transparent decoding** of `gzip` and `deflate` out of the box; `br` and
  `zstd` when the optional libraries are installed.
- **DNS cache with Happy Eyeballs v2** — IPv6/IPv4 races with a 250 ms
  stagger, bounded-TTL cache.
- **Retry and circuit breaker** with error classification, decorrelated-jitter
  backoff, and `Retry-After` support.
- **Cookies, redirects, streaming bodies, JSON via `orjson` when available.**

## Quick example

```python
import asyncio
import hyperhttp

async def main():
    async with hyperhttp.Client() as client:
        response = await client.get("https://example.com")
        await response.aread()
        print(response.status_code, response.text[:120])

asyncio.run(main())
```

## Benchmarks

Measured against `aiohttp` and `httpx` on a local `aiohttp` loopback server
(no network, no DNS, no TLS), 2 000 requests per (client, body size),
concurrency 64.

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
faster than `httpx` on this workload. See [Performance Tips](performance.md)
for ways to squeeze more out of it.

## Next steps

- [Installation](installation.md)
- [Quick Start](quickstart.md)
- [Basic Usage](usage.md)
- [Advanced Features](advanced.md)
- [API Reference](api/client.md)
