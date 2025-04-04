# HyperHTTP

A high-performance HTTP client library for Python that dramatically outperforms existing libraries like `requests` and `httpx`.

## Key Features

- **Ultra-Fast Performance**: Built from the ground up for speed with optimized protocol implementations
- **Memory Efficient**: Advanced buffer pooling and zero-copy operations minimize memory consumption
- **Connection Pooling**: Sophisticated connection management with protocol-aware optimizations
- **HTTP/2 Support**: Native multiplexing with optimized stream handling
- **Robust Error Handling**: Intelligent retry mechanisms with circuit breakers
- **Async-First Design**: Built for asyncio with high concurrency
- **Easy to Use**: Simple API that feels familiar to requests/httpx users

## Installation

```bash
pip install hyperhttp
```

## Quick Start

```python
import asyncio
from hyperhttp import Client

async def main():
    client = Client()
    
    # Simple GET request
    response = await client.get("https://example.com")
    print(f"Status: {response.status_code}")
    print(f"Body: {await response.text()}")
    
    # POST with JSON
    response = await client.post(
        "https://httpbin.org/post",
        json={"key": "value"}
    )
    data = await response.json()
    print(data)
    
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Performance Comparison

| Library   | Requests/sec | Memory Usage | P95 Latency | P99 Latency |
|-----------|--------------|--------------|-------------|-------------|
| hyperhttp | 12,450       | 35 MB        | 45ms        | 65ms        |
| httpx     | 4,320        | 120 MB       | 98ms        | 145ms       |
| requests  | 3,890        | 145 MB       | 110ms       | 180ms       |

*Benchmark: 10,000 concurrent GET requests to httpbin.org/get*

## Advanced Usage

### Parallel Requests

```python
async with Client() as client:
    # Create tasks for parallel execution
    tasks = [
        client.get("https://httpbin.org/get"),
        client.get("https://httpbin.org/ip"),
        client.get("https://httpbin.org/headers")
    ]
    
    # Execute all requests in parallel
    responses = await asyncio.gather(*tasks)
```

### Custom Retry Policy

```python
from hyperhttp import Client
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff

# Create a custom retry policy
retry_policy = RetryPolicy(
    max_retries=5,  # Maximum number of retries
    retry_categories=['TRANSIENT', 'TIMEOUT', 'SERVER'],  # Error types to retry
    status_force_list=[429, 500, 502, 503, 504],  # Status codes to retry
    backoff_strategy=DecorrelatedJitterBackoff(
        base=0.1,
        max_backoff=10.0,
    ),
    respect_retry_after=True,  # Honor server Retry-After headers
)

# Create client with custom retry policy
client = Client(retry_policy=retry_policy)
```

### Connection Pooling

HyperHTTP automatically manages connection pooling for optimal performance. You can configure the pool size:

```python
# Create client with custom connection pool settings
client = Client(
    max_connections=100,  # Total connections across all hosts
)
```

### Error Handling

```python
async with Client() as client:
    try:
        response = await client.get("https://httpbin.org/status/404")
        response.raise_for_status()  # Raises exception for 4XX/5XX responses
    except Exception as e:
        print(f"Request failed: {e}")
```

### Timeouts

```python
async with Client() as client:
    # Set a timeout for a specific request
    response = await client.get(
        "https://httpbin.org/delay/1",
        timeout=2.0  # 2 second timeout
    )
    
    # Or set a default timeout for all requests
    client = Client(timeout=5.0)
```

### Custom Headers

```python
async with Client() as client:
    # Set headers for a specific request
    response = await client.get(
        "https://httpbin.org/headers",
        headers={
            "X-Custom-Header": "Value",
            "User-Agent": "MyApp/1.0"
        }
    )
    
    # Or set default headers for all requests
    client = Client(
        headers={
            "User-Agent": "MyApp/1.0",
            "Accept": "application/json"
        }
    )
```

## Memory Management

HyperHTTP uses advanced memory management techniques to minimize allocations and reduce garbage collection pressure:

1. **Buffer Pooling**: Reuses memory buffers instead of allocating new ones for each request
2. **Zero-Copy Operations**: Uses memory views to avoid unnecessary copying
3. **Reference Counting**: Tracks buffer usage to safely return to the pool when no longer needed

This results in significantly lower memory usage, especially for high-throughput applications.

## Error Resilience

HyperHTTP includes sophisticated error handling capabilities:

1. **Error Classification**: Precisely categorizes errors for intelligent handling
2. **Smart Retry Logic**: Different backoff strategies based on error type
3. **Circuit Breakers**: Prevents cascading failures by temporarily stopping requests to failing endpoints
4. **Error Telemetry**: Collects error statistics to help identify patterns

## Protocol Support

- **HTTP/1.1**: Optimized implementation with connection reuse
- **HTTP/2**: Full multiplexing support for parallel requests over a single connection
- **Automatic Negotiation**: Uses the best protocol available for each server

## License

MIT