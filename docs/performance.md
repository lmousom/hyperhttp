# Performance Tips

This guide provides best practices and tips for optimizing your application's performance with HyperHTTP.

## Connection Management

### Connection Pooling

Use connection pooling to reuse connections and reduce overhead:

```python
client = Client(
    max_connections=100,           # Adjust based on your needs
    max_keepalive_connections=20,  # Keep connections alive
    max_keepalive=300         # 5 minutes
)
```

### HTTP/2 Multiplexing

Enable HTTP/2 for better performance with multiple requests:

```python
# HTTP/2 is enabled by default
client = Client(http2_only=True)  # Force HTTP/2 for all requests
```

## Memory Optimization

### Buffer Pooling

Use buffer pooling to reduce memory allocations:

```python
from hyperhttp.utils.memory import BufferPool

client = Client(
    buffer_pool=BufferPool(
        initial_size=8192,     # 8KB initial size
        max_size=1024*1024,    # 1MB maximum
        pool_size=100          # Pool size
    )
)
```

### Streaming Responses

Stream large responses to avoid loading them entirely into memory:

```python
async with Client() as client:
    async with client.stream("https://example.com/large-file") as response:
        async for chunk in response.iter_chunks(chunk_size=8192):
            process_chunk(chunk)
```

## Concurrent Requests

### Using asyncio.gather

Make multiple requests concurrently:

```python
async def fetch_all(urls):
    async with Client() as client:
        tasks = [client.get(url) for url in urls]
        responses = await asyncio.gather(*tasks)
        return responses
```

### Batch Processing

Process large numbers of requests in batches:

```python
async def process_in_batches(urls, batch_size=10):
    async with Client() as client:
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]
            tasks = [client.get(url) for url in batch]
            responses = await asyncio.gather(*tasks)
            process_batch(responses)
```

## Request Optimization

### Keep-Alive Headers

Set appropriate keep-alive headers:

```python
headers = {
    "Connection": "keep-alive",
    "Keep-Alive": "timeout=300, max=1000"
}

client = Client(headers=headers)
```

### Compression

Enable compression to reduce data transfer:

```python
headers = {
    "Accept-Encoding": "gzip, deflate"
}

client = Client(headers=headers)
```

## Error Handling

### Circuit Breakers

Use circuit breakers to prevent cascading failures:

```python
from hyperhttp.utils.circuit import CircuitBreaker

circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0
)

client = Client(circuit_breaker=circuit_breaker)
```

### Smart Retries

Configure retries with exponential backoff:

```python
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import ExponentialBackoff

retry_policy = RetryPolicy(
    max_retries=3,
    backoff_strategy=ExponentialBackoff(
        initial=0.1,
        max_backoff=10.0
    )
)

client = Client(retry_policy=retry_policy)
```

## Monitoring

### Performance Metrics

Monitor your application's performance:

```python
from hyperhttp.utils.metrics import MetricsCollector

metrics = MetricsCollector()
client = Client(metrics=metrics)

# Periodically check metrics
async def monitor_performance():
    while True:
        print(f"Success rate: {metrics.success_rate}%")
        print(f"Average latency: {metrics.avg_response_time}ms")
        await asyncio.sleep(60)
```

### Request Tracing

Enable request tracing for debugging:

```python
from hyperhttp.utils.tracing import RequestTracer

async def trace_callback(trace_data):
    if trace_data.duration > 1.0:  # Log slow requests
        print(f"Slow request to {trace_data.url}: {trace_data.duration}s")

client = Client(tracer=RequestTracer(callback=trace_callback))
```

## Best Practices

1. **Connection Pooling**: Always use connection pooling for multiple requests to the same host.
2. **HTTP/2**: Use HTTP/2 when possible for better multiplexing and performance.
3. **Streaming**: Stream large responses instead of loading them into memory.
4. **Concurrent Requests**: Use `asyncio.gather` for parallel requests.
5. **Buffer Pooling**: Enable buffer pooling to reduce memory allocations.
6. **Circuit Breakers**: Implement circuit breakers for external services.
7. **Monitoring**: Use metrics and tracing to identify performance issues.
8. **Compression**: Enable compression to reduce data transfer.
9. **Batch Processing**: Process large numbers of requests in batches.
10. **Error Handling**: Implement proper retry policies and error handling.

## Performance Checklist

- [ ] Connection pooling configured appropriately
- [ ] HTTP/2 enabled where supported
- [ ] Buffer pooling implemented
- [ ] Streaming used for large responses
- [ ] Concurrent requests optimized
- [ ] Circuit breakers configured
- [ ] Retry policies implemented
- [ ] Compression enabled
- [ ] Monitoring and metrics in place
- [ ] Regular performance testing 