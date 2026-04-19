"""
RetryHandler and DomainCircuitBreakerManager behaviors.
"""

from __future__ import annotations

import asyncio

import pytest

from hyperhttp.errors.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitBreakerState,
    DomainCircuitBreakerManager,
)
from hyperhttp.errors.retry import RetryError, RetryHandler, RetryPolicy
from hyperhttp.exceptions import ConnectError, ReadTimeout


async def test_retry_passes_original_exception_when_no_retry() -> None:
    """With max_retries=0 we must not wrap — caller sees the typed error."""
    handler = RetryHandler(retry_policy=RetryPolicy(max_retries=0))

    async def boom(*, method: str, url: str, **_: object) -> None:
        raise ConnectError("boom")

    with pytest.raises(ConnectError, match="boom"):
        await handler.execute(boom, method="GET", url="http://x/", domain="x")


async def test_retry_wraps_in_retry_error_after_retries() -> None:
    handler = RetryHandler(retry_policy=RetryPolicy(max_retries=2))

    calls = 0

    async def boom(*, method: str, url: str, **_: object) -> None:
        nonlocal calls
        calls += 1
        raise ReadTimeout("slow")

    with pytest.raises(RetryError) as ei:
        await handler.execute(boom, method="GET", url="http://x/", domain="x")

    assert calls == 3
    assert isinstance(ei.value.original_exception, ReadTimeout)
    assert ei.value.retry_state.attempt_count == 3


async def test_retry_returns_on_eventual_success() -> None:
    handler = RetryHandler(retry_policy=RetryPolicy(max_retries=3))

    calls = 0

    async def flaky(*, method: str, url: str, **_: object) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ReadTimeout("slow")
        return "ok"

    assert await handler.execute(flaky, method="GET", url="http://x/", domain="x") == "ok"
    assert calls == 3


async def test_circuit_breaker_trips_and_recovers() -> None:
    mgr = DomainCircuitBreakerManager(
        default_config={
            "failure_threshold": 2,
            "recovery_timeout": 0.05,
            "success_threshold": 1,
        }
    )

    async def boom() -> None:
        # ReadTimeout classifies as TIMEOUT which is tracked by the CB.
        raise ReadTimeout("slow")

    for _ in range(2):
        with pytest.raises(ReadTimeout):
            await mgr.execute("x", boom)

    # Circuit is now OPEN — next call short-circuits.
    cb = await mgr.get_circuit_breaker("x")
    assert cb.state == CircuitBreakerState.OPEN
    with pytest.raises(CircuitBreakerOpenError):
        await mgr.execute("x", boom)

    await asyncio.sleep(0.06)  # past the recovery window

    async def happy() -> str:
        return "ok"

    assert await mgr.execute("x", happy) == "ok"
    assert cb.state == CircuitBreakerState.CLOSED


async def test_circuit_breaker_configure_domain_is_atomic() -> None:
    mgr = DomainCircuitBreakerManager()
    await mgr.configure_domain("y", failure_threshold=1, recovery_timeout=0.01)
    cb = await mgr.get_circuit_breaker("y")
    assert cb._failure_threshold == 1
