# Retry Policy API Reference

## `hyperhttp.errors.retry.RetryPolicy`

```python
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import BackoffStrategy

class RetryPolicy:
    def __init__(
        self,
        max_retries: int = 3,
        retry_categories: list[str] | None = None,     # default: ["TRANSIENT", "TIMEOUT", "SERVER"]
        status_force_list: list[int] | None = None,    # default: [429, 500, 502, 503, 504]
        backoff_strategy: BackoffStrategy | None = None,  # default: ExponentialBackoff()
        respect_retry_after: bool = True,
        retry_interval_factor: float = 1.0,
    ) -> None: ...
```

`max_retries` counts retries, **not** total attempts. With the default
`max_retries=3`, the client may attempt the request up to four times.

### Arguments

| Argument                | Description                                                                |
|-------------------------|----------------------------------------------------------------------------|
| `max_retries`           | Maximum number of retries after the initial attempt.                       |
| `retry_categories`      | Error categories that trigger a retry. See below.                          |
| `status_force_list`     | HTTP status codes that force a retry even when the category wouldn't.      |
| `backoff_strategy`      | Delay between retries. Default: `ExponentialBackoff()`.                    |
| `respect_retry_after`   | Honor the `Retry-After` header on 429/503 responses.                       |
| `retry_interval_factor` | Multiplier applied on top of the backoff strategy (useful for test tuning).|

### Retry categories

| Category     | Matches                                                             |
|--------------|---------------------------------------------------------------------|
| `CONNECTION` | `ConnectError`, `DNSError`                                          |
| `TIMEOUT`    | `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout`      |
| `TRANSIENT`  | `ReadError`, `WriteError`, `ProtocolError`                          |
| `SERVER`     | `HTTPStatusError` 5xx                                               |
| `RATE_LIMIT` | `HTTPStatusError` 429                                               |
| `FATAL`      | `InvalidURL`, `TooManyRedirects`, non-429 4xx                       |

`FATAL` is never retried. The default `retry_categories` is
`["TRANSIENT", "TIMEOUT", "SERVER"]`.

## Backoff strategies

Importable from `hyperhttp.utils.backoff`.

### `ExponentialBackoff`

```python
class ExponentialBackoff(BackoffStrategy):
    def __init__(
        self,
        base: float = 0.5,       # initial delay (seconds)
        factor: float = 2.0,     # multiplier per attempt
        max_backoff: float = 60.0,
        jitter: bool = True,     # 0.8x–1.2x randomization
    ) -> None: ...
```

Delay for attempt `n` is roughly `base * factor**n`, capped at `max_backoff`,
optionally jittered.

### `DecorrelatedJitterBackoff`

AWS-style decorrelated jitter. Spreads retries more evenly under contention
than classic exponential backoff.

```python
class DecorrelatedJitterBackoff(BackoffStrategy):
    def __init__(
        self,
        base: float = 0.5,
        max_backoff: float = 60.0,
        jitter_cap: float | None = None,
    ) -> None: ...
```

### `AdaptiveBackoff`

Adjusts base delay based on recent success/failure feedback. Useful when
downstream behaviour shifts over time:

```python
from hyperhttp.utils.backoff import AdaptiveBackoff

backoff = AdaptiveBackoff(base=0.1, max_backoff=10.0)
```

## Using a retry policy

```python
import hyperhttp
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import DecorrelatedJitterBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"],
    status_force_list=[429, 500, 502, 503, 504],
    backoff_strategy=DecorrelatedJitterBackoff(base=0.1, max_backoff=10.0),
    respect_retry_after=True,
)

async with hyperhttp.Client(retry=retry_policy) as client:
    response = await client.get("https://api.example.com/things")
```

### Disabling retry per request

The client-level retry policy applies to every request by default. Opt out on
a per-call basis:

```python
await client.post(url, json=payload, retry=False)
```

### Opting into defaults without building a policy

```python
client = hyperhttp.Client(retry=True)  # RetryPolicy() with defaults
```

## `RetryError`

Raised once at least one retry has been attempted and the request is still
failing.

```python
from hyperhttp.errors.retry import RetryError

try:
    await client.get("https://flaky.example.com")
except RetryError as e:
    print(e.original_exception)       # e.g. ReadTimeout("...")
    print(e.retry_state.attempt_count)
    print(e.retry_state.total_delay)  # seconds spent waiting
    print(e.retry_state.elapsed)      # wall-clock seconds since first attempt
```

The **initial** failure is never wrapped in `RetryError` — callers see the
typed transport/timeout exception directly. Only subsequent failures after at
least one retry surface as `RetryError`.

## Telemetry

Hook into every attempt for metrics or logging:

```python
from hyperhttp.errors.telemetry import ErrorTelemetry

class LoggingTelemetry(ErrorTelemetry):
    def record_attempt(self, retry_state, outcome: str) -> None:
        # outcome ∈ {"success", "retry", "fail"}
        print(
            outcome,
            retry_state.method,
            retry_state.url,
            retry_state.attempt_count,
            retry_state.last_error_category,
        )

client = hyperhttp.Client(retry=True, telemetry=LoggingTelemetry())
```
