"""
Circuit breaker pattern implementation for preventing cascading failures.
"""

import asyncio
import logging
import time
from typing import Dict, Any, Optional, Callable, TypeVar, Awaitable, Set

from hyperhttp.errors.classifier import ErrorClassifier

# Logger
logger = logging.getLogger("hyperhttp.errors.circuit_breaker")

# Type for coroutine functions
T = TypeVar("T")
CoroFunc = Callable[..., Awaitable[T]]


class CircuitBreakerState:
    """Circuit breaker state enumeration."""
    CLOSED = 'CLOSED'       # Normal operation
    OPEN = 'OPEN'           # Failing, not allowing requests
    HALF_OPEN = 'HALF_OPEN' # Testing if system has recovered


class CircuitBreakerOpenError(Exception):
    """Exception raised when a circuit breaker is open."""
    pass


class CircuitBreaker:
    """
    Circuit breaker for preventing cascading failures.
    
    This implements the circuit breaker pattern to stop sending requests
    to failing services until they recover.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
        tracked_categories: Optional[Set[str]] = None,
    ):
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._last_success_time = 0.0
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._success_threshold = success_threshold
        self._tracked_categories = tracked_categories or {
            'SERVER', 'TIMEOUT', 'TRANSIENT'
        }
        self._lock = asyncio.Lock()
        
    async def execute(self, coro_func: CoroFunc, *args: Any, **kwargs: Any) -> Any:
        """
        Execute a coroutine with circuit breaker protection.
        
        Args:
            coro_func: Coroutine function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function
            
        Returns:
            Function result
            
        Raises:
            CircuitBreakerOpenError: If circuit is open
        """
        await self._check_state()
        
        try:
            result = await coro_func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            category = ErrorClassifier.categorize(e)
            if category in self._tracked_categories:
                await self._on_failure(e, category)
            raise
            
    async def _check_state(self) -> None:
        """
        Check if requests can be executed based on circuit state.
        
        Raises:
            CircuitBreakerOpenError: If circuit is open
        """
        async with self._lock:
            if self._state == CircuitBreakerState.OPEN:
                # Check if recovery timeout has elapsed
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    # Move to HALF_OPEN to test if system has recovered
                    logger.info("Circuit breaker moving from OPEN to HALF_OPEN")
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._success_count = 0
                else:
                    # Still in timeout period
                    timeout_remaining = self._last_failure_time + self._recovery_timeout - time.monotonic()
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker is OPEN for {timeout_remaining:.1f}s more"
                    )
                    
    async def _on_success(self) -> None:
        """Handle successful request execution."""
        async with self._lock:
            self._last_success_time = time.monotonic()
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    # System has recovered, reset to normal operation
                    logger.info("Circuit breaker moving from HALF_OPEN to CLOSED")
                    self._state = CircuitBreakerState.CLOSED
                    self._failure_count = 0
                    
    async def _on_failure(self, exception: Exception, category: str) -> None:
        """
        Handle request failure.
        
        Args:
            exception: Exception that occurred
            category: Error category
        """
        async with self._lock:
            self._last_failure_time = time.monotonic()
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                # Failed during testing period, reopen circuit
                logger.warning(
                    f"Circuit breaker moving from HALF_OPEN to OPEN due to {category} error"
                )
                self._state = CircuitBreakerState.OPEN
            elif self._state == CircuitBreakerState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    # Too many failures, open circuit
                    logger.warning(
                        f"Circuit breaker moving from CLOSED to OPEN after {self._failure_count} failures"
                    )
                    self._state = CircuitBreakerState.OPEN
    
    @property
    def state(self) -> str:
        """Get the current circuit breaker state."""
        return self._state
    
    @property
    def failure_count(self) -> int:
        """Get the current failure count."""
        return self._failure_count


class DomainCircuitBreakerManager:
    """
    Manager for domain-specific circuit breakers.
    
    This maintains separate circuit breakers for different domains,
    allowing fine-grained failure handling.
    """
    
    def __init__(self, default_config: Optional[Dict[str, Any]] = None):
        self._domain_breakers: Dict[str, CircuitBreaker] = {}
        self._default_config = default_config or {
            'failure_threshold': 5,
            'recovery_timeout': 30.0,
            'success_threshold': 2,
        }
        self._lock = asyncio.Lock()
        
    async def get_circuit_breaker(self, domain: str) -> CircuitBreaker:
        """
        Get the circuit breaker for a domain.
        
        Args:
            domain: Domain name
            
        Returns:
            CircuitBreaker for the domain
        """
        async with self._lock:
            if domain not in self._domain_breakers:
                # Create circuit breaker for this domain
                self._domain_breakers[domain] = CircuitBreaker(
                    **self._default_config
                )
            return self._domain_breakers[domain]
            
    async def execute(
        self,
        domain: str,
        coro_func: CoroFunc,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Execute a coroutine with domain-specific circuit breaker protection.
        
        Args:
            domain: Domain name
            coro_func: Coroutine function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function
            
        Returns:
            Function result
        """
        breaker = await self.get_circuit_breaker(domain)
        return await breaker.execute(coro_func, *args, **kwargs)
            
    def configure_domain(self, domain: str, **config: Any) -> None:
        """
        Configure a domain-specific circuit breaker.
        
        Args:
            domain: Domain name
            **config: Circuit breaker configuration
        """
        breaker_config = self._default_config.copy()
        breaker_config.update(config)
        
        circuit_breaker = CircuitBreaker(**breaker_config)
        self._domain_breakers[domain] = circuit_breaker
    
    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Get statistics for all circuit breakers.
        
        Returns:
            Dictionary of domain to circuit breaker statistics
        """
        return {
            domain: {
                'state': breaker.state,
                'failure_count': breaker.failure_count,
            }
            for domain, breaker in self._domain_breakers.items()
        }