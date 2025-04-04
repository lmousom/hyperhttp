"""
Retry mechanisms for HTTP requests.
"""

import asyncio
import email.utils
import logging
import random
import time
import uuid
from typing import Dict, Any, Optional, List, Tuple, Type, Callable, Union

from hyperhttp.errors.classifier import ErrorClassifier
from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager
from hyperhttp.utils.backoff import BackoffStrategy, ExponentialBackoff

# Logger
logger = logging.getLogger("hyperhttp.errors.retry")


class RetryState:
    """
    State tracking for request retries.
    
    This maintains context across retry attempts to inform retry decisions
    and enable request adaptation.
    """
    
    def __init__(self, method: str, url: str, original_kwargs: Dict[str, Any]):
        self.method = method
        self.url = url
        self.original_kwargs = original_kwargs
        self.attempts: List[Dict[str, Any]] = []
        self.start_time = time.time()
        self.request_id = str(uuid.uuid4())
        self.modified_kwargs: Dict[str, Any] = {}
        
    @property
    def attempt_count(self) -> int:
        """Get the number of retry attempts so far."""
        return len(self.attempts)
        
    @property
    def last_error_category(self) -> Optional[str]:
        """Get the category of the most recent error."""
        if not self.attempts:
            return None
        return self.attempts[-1].get('category')
        
    @property
    def total_delay(self) -> float:
        """Get the total backoff delay across all retries."""
        return sum(attempt.get('backoff', 0) for attempt in self.attempts)
        
    @property
    def elapsed(self) -> float:
        """Get the total elapsed time since the first request."""
        return time.time() - self.start_time


class RetryError(Exception):
    """
    Exception raised when all retry attempts have failed.
    
    This preserves the original exception and retry state for
    inspection and handling.
    """
    
    def __init__(
        self,
        message: str,
        original_exception: Exception,
        retry_state: RetryState,
    ):
        self.message = message
        self.original_exception = original_exception
        self.retry_state = retry_state
        super().__init__(message)


class RetryPolicy:
    """
    Policy for determining when and how to retry requests.
    
    This configures the retry behavior based on error categories,
    status codes, and backoff strategies.
    """
    
    def __init__(
        self,
        max_retries: int = 3,
        retry_categories: Optional[List[str]] = None,
        status_force_list: Optional[List[int]] = None,
        backoff_strategy: Optional[BackoffStrategy] = None,
        respect_retry_after: bool = True,
        retry_interval_factor: float = 1.0,
    ):
        self.max_retries = max_retries
        self.retry_categories = retry_categories or [
            'TRANSIENT', 'TIMEOUT', 'SERVER'
        ]
        self.status_force_list = status_force_list or [
            429, 500, 502, 503, 504
        ]
        self.backoff_strategy = backoff_strategy or ExponentialBackoff()
        self.respect_retry_after = respect_retry_after
        self.retry_interval_factor = retry_interval_factor
        
    def should_retry(
        self,
        error: Exception,
        response: Optional[Any] = None,
        retry_count: int = 0,
    ) -> Tuple[bool, float]:
        """
        Determine if a request should be retried.
        
        Args:
            error: Exception that occurred
            response: Optional response object
            retry_count: Number of retries so far
            
        Returns:
            Tuple of (should_retry, backoff_time)
        """
        if retry_count >= self.max_retries:
            return False, 0  # Exceeded max retries
            
        # Get error category
        category = ErrorClassifier.categorize(error, response)
        
        # Check if this category is retryable
        if category not in self.retry_categories:
            return False, 0
            
        # Special handling for HTTP status codes
        if response and hasattr(response, 'status_code'):
            status_code = response.status_code
            
            if status_code not in self.status_force_list:
                # Status code not in our retry list
                if not ErrorClassifier.is_retryable(category):
                    return False, 0
                    
            # Check for Retry-After header if configured to respect it
            if self.respect_retry_after and hasattr(response, 'headers'):
                retry_after = response.headers.get('retry-after')
                if retry_after:
                    retry_time = self._parse_retry_after(retry_after)
                    if retry_time:
                        return True, retry_time
        
        # Calculate backoff time using strategy
        backoff_time = self.backoff_strategy.calculate_backoff(retry_count)
        
        # Apply custom factor (for testing or tuning)
        backoff_time *= self.retry_interval_factor
        
        return True, backoff_time
        
    def _parse_retry_after(self, retry_after: str) -> Optional[float]:
        """
        Parse a Retry-After header value.
        
        Args:
            retry_after: Retry-After header value
            
        Returns:
            Delay in seconds, or None if invalid
        """
        try:
            # Try parsing as integer seconds
            return float(retry_after)
        except ValueError:
            try:
                # Try parsing as HTTP date
                retry_date = email.utils.parsedate_to_datetime(retry_after)
                now = time.time()
                return max(0, (retry_date.timestamp() - now))
            except Exception:
                # Invalid Retry-After header
                return None


class RequestAdapter:
    """
    Base class for request adapters.
    
    Request adapters modify requests between retry attempts
    based on observed errors to improve success rates.
    """
    
    async def adapt_request(
        self,
        retry_state: RetryState,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Adapt a request based on retry state.
        
        Args:
            retry_state: Current retry state
            kwargs: Request keyword arguments
            
        Returns:
            Modified request kwargs
        """
        # Base implementation does nothing
        return kwargs


class TimeoutAdapter(RequestAdapter):
    """Adapter that increases timeouts for slow requests."""
    
    async def adapt_request(
        self,
        retry_state: RetryState,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not retry_state.attempts:
            return kwargs
            
        # Handle timeout-related errors
        categories = [attempt.get('category') for attempt in retry_state.attempts]
        
        if 'TIMEOUT' in categories:
            # Increase timeout on timeout errors
            current_timeout = kwargs.get('timeout', 30.0)
            kwargs['timeout'] = min(current_timeout * 1.5, 120.0)  # Cap at 2 minutes
            
        return kwargs


class RetryHandler:
    """
    Handler for executing requests with retry logic.
    
    This combines retry policies, circuit breakers, and request
    adapters to provide robust error handling.
    """
    
    def __init__(
        self,
        client: Any,
        retry_policy: Optional[RetryPolicy] = None,
        circuit_breaker_manager: Optional[DomainCircuitBreakerManager] = None,
        request_adapters: Optional[List[RequestAdapter]] = None,
    ):
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy()
        self._circuit_breakers = circuit_breaker_manager or DomainCircuitBreakerManager()
        self._request_adapters = request_adapters or [TimeoutAdapter()]
        
    async def execute_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        """
        Execute a request with retry logic.
        
        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments
            
        Returns:
            Response object
            
        Raises:
            RetryError: If all retry attempts fail
        """
        # Parse domain for circuit breaker
        from urllib.parse import urlparse
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        # Track retry attempts and last error
        retry_count = 0
        last_exception = None
        last_response = None
        
        # Configure context tracking
        retry_state = RetryState(
            method=method,
            url=url,
            original_kwargs=kwargs.copy()
        )
        
        while True:
            try:
                # Execute with circuit breaker
                return await self._circuit_breakers.execute(
                    domain,
                    self._client._execute_request,
                    method, url, **kwargs
                )
            except Exception as e:
                last_exception = e
                
                # Extract response if this is a response error
                if hasattr(e, 'response'):
                    last_response = e.response
                
                # Check if we should retry
                should_retry, backoff_time = self._retry_policy.should_retry(
                    e, last_response, retry_count
                )
                
                if not should_retry:
                    # No more retries, re-raise the exception
                    raise RetryError(
                        f"Failed after {retry_count} retries: {str(e)}",
                        original_exception=e,
                        retry_state=retry_state
                    ) from e
                    
                # Update retry state
                retry_state.attempts.append({
                    'timestamp': time.time(),
                    'exception': str(e),
                    'category': ErrorClassifier.categorize(e, last_response),
                    'backoff': backoff_time,
                    'response': last_response,
                })
                
                # Log retry attempt
                logger.info(
                    f"Retrying {method} {url} after error: {str(e)}, "
                    f"retry {retry_count+1} in {backoff_time:.2f}s"
                )
                
                # Apply backoff delay
                await asyncio.sleep(backoff_time)
                
                # Increment retry counter
                retry_count += 1
                
                # Allow request modification before retry
                kwargs = await self._prepare_retry(retry_state, kwargs)
    
    async def _prepare_retry(
        self,
        retry_state: RetryState,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Prepare a request for retry, applying all adapters.
        
        Args:
            retry_state: Current retry state
            kwargs: Request keyword arguments
            
        Returns:
            Modified request kwargs
        """
        modified_kwargs = kwargs.copy()
        
        # Apply all request adapters
        for adapter in self._request_adapters:
            modified_kwargs = await adapter.adapt_request(retry_state, modified_kwargs)
            
        return modified_kwargs