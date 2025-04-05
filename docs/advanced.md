# Advanced Features

This guide covers advanced features and configurations in HyperHTTP. Make sure you're familiar with the [basic usage](usage.md) before diving into these topics.

## HTTP/2 Support

HyperHTTP provides native HTTP/2 support with multiplexing:

```python
from hyperhttp import Client

# HTTP/2 is enabled by default
client = Client()

# Force HTTP/2 only
client = Client(http2_only=True)

# Disable HTTP/2
client = Client(enable_http2=False)
```

### Stream Multiplexing

HTTP/2 allows multiple requests to share a single connection:

```python
async with Client() as client:
    # These requests will be multiplexed over a single connection
    responses = await asyncio.gather(
        client.get("https://example.com/api/1"),
        client.get("https://example.com/api/2"),
        client.get("https://example.com/api/3")
    )
```

## Connection Pooling

### Pool Configuration

```python
client = Client(
    max_connections=100,           # Total connections
    max_keepalive_connections=20,  # Connections to keep alive
    max_keepalive=300,        # Timeout in seconds
)
```

### Connection Reuse

```python
async with Client() as client:
    # These requests will reuse connections when possible
    for i in range(10):
        response = await client.get(f"https://api.example.com/item/{i}")
```

## Advanced Retry Strategies

### Custom Retry Policy

```python
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import ExponentialBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"],
    status_force_list=[429, 500, 502, 503, 504],
    backoff_strategy=ExponentialBackoff(
        initial=0.1,
        multiplier=2.0,
        max_backoff=30.0,
        jitter=True
    ),
    respect_retry_after=True
)

client = Client(retry_policy=retry_policy)
```

### Custom Retry Conditions

```python
def should_retry(response):
    # Custom logic to determine if retry is needed
    return response.status_code == 418  # I'm a teapot

retry_policy = RetryPolicy(
    max_retries=3,
    retry_if_result=should_retry
)
```

## Memory Management

### Buffer Pooling

```python
from hyperhttp.utils.memory import BufferPool

client = Client(
    buffer_pool=BufferPool(
        initial_size=1024,    # Initial buffer size
        max_size=1024*1024,   # Maximum buffer size
        pool_size=100         # Number of buffers to keep
    )
)
```

### Zero-Copy Operations

```python
async with Client() as client:
    response = await client.get("https://example.com/large-file")
    
    # Stream response without loading into memory
    async with response.stream() as stream:
        async for chunk in stream:
            process_chunk(chunk)  # Process each chunk
```

## Circuit Breakers

### Basic Circuit Breaker

```python
from hyperhttp.utils.circuit import CircuitBreaker

circuit_breaker = CircuitBreaker(
    failure_threshold=5,     # Number of failures before opening
    recovery_timeout=30.0,   # Time to wait before half-open
    success_threshold=2      # Successes needed to close
)

client = Client(circuit_breaker=circuit_breaker)
```

### Per-Host Circuit Breakers

```python
from hyperhttp.utils.circuit import HostCircuitBreaker

circuit_breaker = HostCircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    success_threshold=2,
    max_hosts=100  # Maximum number of hosts to track
)

client = Client(circuit_breaker=circuit_breaker)
```

## Custom Transport Layer

### Custom SSL Configuration

```python
import ssl

ssl_context = ssl.create_default_context()
ssl_context.load_cert_chain("client-cert.pem", "client-key.pem")
ssl_context.load_verify_locations("ca-cert.pem")

client = Client(ssl_context=ssl_context)
```

### Custom DNS Resolution

```python
from hyperhttp.utils.dns import CustomResolver

resolver = CustomResolver(
    nameservers=["8.8.8.8", "8.8.4.4"],
    cache_size=1000,
    ttl=300
)

client = Client(resolver=resolver)
```

## Monitoring and Metrics

### Request Tracing

```python
from hyperhttp.utils.tracing import RequestTracer

async def trace_callback(trace_data):
    print(f"Request to {trace_data.url} took {trace_data.duration}s")

tracer = RequestTracer(callback=trace_callback)
client = Client(tracer=tracer)
```

### Performance Metrics

```python
from hyperhttp.utils.metrics import MetricsCollector

metrics = MetricsCollector()
client = Client(metrics=metrics)

# Later, get the metrics
print(f"Average response time: {metrics.avg_response_time}ms")
print(f"Success rate: {metrics.success_rate}%")
print(f"Active connections: {metrics.active_connections}")
```

## Next Steps

- Check out the [Performance Tips](performance.md) for optimizing your applications
- Read the [API Reference](api/client.md) for detailed documentation
- Learn about [Error Handling](api/errors.md) for robust applications 