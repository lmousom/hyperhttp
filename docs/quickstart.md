# Quick Start

## Install

```bash
pip install 'hyperhttp[speed]'
```

See the [Installation](installation.md) page for details on optional extras.

## A single request

```python
import asyncio
import hyperhttp

async def main():
    async with hyperhttp.Client() as client:
        response = await client.get("https://example.com")
        await response.aread()
        print(response.status_code)
        print(response.text[:200])

asyncio.run(main())
```

The `Client` is async-context-managed so the connection pool is closed cleanly.
`response.aread()` materializes the body into `response.content`; after that
`response.text` and `response.json()` are cheap synchronous accessors.

## POST JSON and read a JSON reply

```python
import asyncio
import hyperhttp

async def main():
    async with hyperhttp.Client() as client:
        response = await client.post(
            "https://httpbin.org/post",
            json={"hello": "world"},
        )
        await response.aread()
        print(response.json())

asyncio.run(main())
```

## Streaming a large body

```python
async with hyperhttp.Client() as client:
    response = await client.get("https://example.com/large.bin")
    async for chunk in response.aiter_bytes():
        handle(chunk)
```

Iterating a response implicitly closes it when the iterator is exhausted — the
connection goes back to the pool automatically.

## Concurrent requests

```python
import asyncio
import hyperhttp

async def main():
    async with hyperhttp.Client() as client:
        urls = [
            "https://httpbin.org/get",
            "https://httpbin.org/ip",
            "https://httpbin.org/headers",
        ]
        responses = await asyncio.gather(*(client.get(u) for u in urls))
        for r in responses:
            await r.aread()
            print(r.status_code, r.url)

asyncio.run(main())
```

If all three hosts support HTTP/2, they're multiplexed over a single TCP
connection per host.

## Enable uvloop

For maximum throughput, drop uvloop in as the event loop:

```python
import hyperhttp

hyperhttp.install_uvloop()  # call once, at program start
```

This is a no-op when `uvloop` is not installed, so it's safe to leave in.

## Next steps

- [Basic Usage](usage.md) — cookies, headers, timeouts, error handling
- [Advanced Features](advanced.md) — retries, circuit breakers, HTTP/2, pooling
- [Performance Tips](performance.md) — squeezing every drop of throughput
- [API Reference](api/client.md)
