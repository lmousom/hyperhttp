# Retry Policy API Reference

This page documents the retry policy functionality and related classes.

## RetryPolicy

```python
class RetryPolicy:
    """
    Configuration for automatic request retries.
    
    Args:
        max_retries (int): Maximum number of retry attempts
        retry_categories (List[str], optional): Categories of errors to retry
        status_force_list (List[int], optional): Status codes to force retry
        backoff_strategy (BackoffStrategy, optional): Strategy for delay between retries
        retry_if_result (Callable[[Response], bool], optional): Custom retry condition
        respect_retry_after (bool, optional): Honor Retry-After headers
    """
    
    def should_retry(self, response: Response, attempt: int) -> bool:
        """
        Determine if a request should be retried.
        
        Args:
            response: The failed response
            attempt: Current attempt number
            
        Returns:
            bool: True if should retry, False otherwise
        """
```

## Backoff Strategies

### Base Strategy

```python
class BackoffStrategy:
    """Base class for backoff strategies."""
    
    def get_delay(self, attempt: int) -> float:
        """
        Calculate delay for the next retry attempt.
        
        Args:
            attempt: Current attempt number
            
        Returns:
            float: Delay in seconds
        """
```

### Exponential Backoff

```python
class ExponentialBackoff(BackoffStrategy):
    """
    Exponential backoff with optional jitter.
    
    Args:
        initial (float): Initial delay in seconds
        multiplier (float): Multiplier for each attempt
        max_backoff (float): Maximum delay in seconds
        jitter (bool): Add random jitter to delay
    """
```

### Decorrelated Jitter

```python
class DecorrelatedJitterBackoff(BackoffStrategy):
    """
    Decorrelated jitter backoff strategy.
    
    Args:
        base (float): Base delay in seconds
        max_backoff (float): Maximum delay in seconds
    """
```

## Retry Categories

The following retry categories are available:

- `"TRANSIENT"`: Temporary network issues
- `"TIMEOUT"`: Request timeouts
- `"SERVER"`: Server errors (5xx)
- `"RATE_LIMIT"`: Rate limiting (429)
- `"CONNECTION"`: Connection errors

## Examples

### Basic Retry Policy

```python
from hyperhttp.errors.retry import RetryPolicy

retry_policy = RetryPolicy(
    max_retries=3,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"]
)

client = Client(retry_policy=retry_policy)
```

### Custom Retry Conditions

```python
def should_retry(response):
    # Retry on specific error message
    if response.status_code == 400:
        data = response.json()
        return "retry_allowed" in data
    return False

retry_policy = RetryPolicy(
    max_retries=3,
    retry_if_result=should_retry
)
```

### Exponential Backoff

```python
from hyperhttp.errors.retry import RetryPolicy
from hyperhttp.utils.backoff import ExponentialBackoff

retry_policy = RetryPolicy(
    max_retries=5,
    backoff_strategy=ExponentialBackoff(
        initial=0.1,      # Start with 0.1 second
        multiplier=2.0,   # Double the delay each time
        max_backoff=30.0, # Maximum 30 seconds
        jitter=True       # Add randomness
    )
)
```

### Status Code Based Retries

```python
retry_policy = RetryPolicy(
    max_retries=3,
    status_force_list=[429, 500, 502, 503, 504]
)
```

### Respecting Retry-After Headers

```python
retry_policy = RetryPolicy(
    max_retries=3,
    respect_retry_after=True  # Honor server's Retry-After header
)
```

### Combined Configuration

```python
retry_policy = RetryPolicy(
    max_retries=5,
    retry_categories=["TRANSIENT", "TIMEOUT", "SERVER"],
    status_force_list=[429, 500, 502, 503, 504],
    backoff_strategy=ExponentialBackoff(
        initial=0.1,
        max_backoff=30.0,
        jitter=True
    ),
    respect_retry_after=True
)
```

## Best Practices

1. **Start Conservative**: Begin with a small number of retries and adjust based on needs
2. **Use Appropriate Categories**: Choose retry categories that match your use case
3. **Add Jitter**: Always use jitter in production to prevent thundering herd
4. **Set Maximum Backoff**: Prevent excessive delays with reasonable max_backoff
5. **Honor Retry-After**: Respect server retry hints when available
6. **Monitor Retries**: Track retry attempts through metrics for optimization 