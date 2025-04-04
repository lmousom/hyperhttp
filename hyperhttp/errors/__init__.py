"""
Error handling and retry mechanisms for HyperHTTP.

This package contains components for robust error classification,
smart retry policies, circuit breakers, and error telemetry.
"""

from hyperhttp.errors.classifier import ErrorClassifier
from hyperhttp.errors.retry import RetryPolicy, RetryState, RetryError, RetryHandler
from hyperhttp.errors.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerState,
    CircuitBreakerOpenError,
    DomainCircuitBreakerManager,
)
from hyperhttp.errors.telemetry import ErrorTelemetry

__all__ = [
    "ErrorClassifier",
    "RetryPolicy",
    "RetryState",
    "RetryError",
    "RetryHandler",
    "CircuitBreaker",
    "CircuitBreakerState",
    "CircuitBreakerOpenError",
    "DomainCircuitBreakerManager",
    "ErrorTelemetry",
]