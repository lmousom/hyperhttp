"""
Retry mechanism.

Notes vs. the previous implementation:

- We never wrap the *original* exception when ``retry_count == 0`` (i.e. the
  request was attempted exactly once and failed). Wrapping would mask the
  typed exception (``ConnectError``, ``ReadTimeout``, ...) the caller cares
  about. We only emit ``RetryError`` after at least one retry has been
  attempted.
- ``ErrorTelemetry.record_error`` is called for every classified failure.
- The retry path is decoupled from any specific ``Client`` shape: callers
  pass an async ``executor`` callable.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from hyperhttp.errors.circuit_breaker import DomainCircuitBreakerManager
from hyperhttp.errors.classifier import ErrorClassifier
from hyperhttp.errors.telemetry import ErrorTelemetry
from hyperhttp.utils.backoff import BackoffStrategy, ExponentialBackoff

logger = logging.getLogger("hyperhttp.errors.retry")


def _safe_url_for_log(url: str) -> str:
    """Strip query string and userinfo from ``url`` before logging.

    Both commonly carry credentials (``?api_key=``, ``user:pass@``). We don't
    want a retry log message to exfiltrate them into shared log aggregators.
    Best-effort: on any parse failure, fall back to scheme://host to keep the
    log useful without the risky bits.
    """
    try:
        from hyperhttp._url import URL

        return URL(url).sanitized()
    except Exception:
        try:
            from urllib.parse import urlparse

            p = urlparse(url)
            return f"{p.scheme}://{p.hostname or ''}{p.path or ''}"
        except Exception:
            return "<url>"

__all__ = [
    "RetryError",
    "RetryPolicy",
    "RetryState",
    "RetryHandler",
    "RequestAdapter",
    "TimeoutAdapter",
]


class RetryState:
    """Per-request retry context shared with adapters."""

    def __init__(self, method: str, url: str, original_kwargs: Dict[str, Any]) -> None:
        self.method = method
        self.url = url
        self.original_kwargs = original_kwargs
        self.attempts: List[Dict[str, Any]] = []
        self.start_time = time.monotonic()
        self.request_id = str(uuid.uuid4())

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_error_category(self) -> Optional[str]:
        return self.attempts[-1]["category"] if self.attempts else None

    @property
    def total_delay(self) -> float:
        return sum(a.get("backoff", 0.0) for a in self.attempts)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time


class RetryError(Exception):
    """Raised after one or more retries have failed.

    The first failure is *not* wrapped; we only surface this once we've
    actually retried, so users still get typed exceptions on initial failure.
    """

    def __init__(
        self,
        message: str,
        original_exception: BaseException,
        retry_state: RetryState,
    ) -> None:
        super().__init__(message)
        self.original_exception = original_exception
        self.retry_state = retry_state


class RetryPolicy:
    def __init__(
        self,
        max_retries: int = 3,
        retry_categories: Optional[List[str]] = None,
        status_force_list: Optional[List[int]] = None,
        backoff_strategy: Optional[BackoffStrategy] = None,
        respect_retry_after: bool = True,
        retry_interval_factor: float = 1.0,
    ) -> None:
        self.max_retries = max_retries
        self.retry_categories = retry_categories or ["TRANSIENT", "TIMEOUT", "SERVER"]
        self.status_force_list = status_force_list or [429, 500, 502, 503, 504]
        self.backoff_strategy = backoff_strategy or ExponentialBackoff()
        self.respect_retry_after = respect_retry_after
        self.retry_interval_factor = retry_interval_factor

    def should_retry(
        self,
        error: BaseException,
        response: Optional[Any] = None,
        retry_count: int = 0,
    ) -> Tuple[bool, float]:
        if retry_count >= self.max_retries:
            return False, 0.0

        category = ErrorClassifier.categorize(error, response)

        if category not in self.retry_categories:
            return False, 0.0

        if response is not None and hasattr(response, "status_code"):
            status_code = response.status_code
            if 400 <= status_code < 600 and status_code not in self.status_force_list:
                if not ErrorClassifier.is_retryable(category):
                    return False, 0.0
            if self.respect_retry_after and hasattr(response, "headers"):
                ra = response.headers.get("retry-after")
                if ra:
                    parsed = self._parse_retry_after(ra)
                    if parsed is not None:
                        return True, parsed

        backoff = self.backoff_strategy.calculate_backoff(retry_count)
        return True, backoff * self.retry_interval_factor

    @staticmethod
    def _parse_retry_after(value: str) -> Optional[float]:
        try:
            return float(value)
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(value)
                return max(0.0, dt.timestamp() - time.time())
            except Exception:
                return None


class RequestAdapter:
    async def adapt_request(
        self, retry_state: RetryState, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        return kwargs


class TimeoutAdapter(RequestAdapter):
    """Bump timeouts after timeout-class failures."""

    async def adapt_request(
        self, retry_state: RetryState, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not retry_state.attempts:
            return kwargs
        if any(a.get("category") == "TIMEOUT" for a in retry_state.attempts):
            current = kwargs.get("timeout") or 30.0
            try:
                kwargs["timeout"] = min(float(current) * 1.5, 120.0)
            except (TypeError, ValueError):
                pass
        return kwargs


Executor = Callable[..., Awaitable[Any]]


class RetryHandler:
    def __init__(
        self,
        *,
        retry_policy: Optional[RetryPolicy] = None,
        circuit_breaker_manager: Optional[DomainCircuitBreakerManager] = None,
        telemetry: Optional[ErrorTelemetry] = None,
        request_adapters: Optional[List[RequestAdapter]] = None,
    ) -> None:
        self._policy = retry_policy or RetryPolicy()
        self._circuit_breakers = circuit_breaker_manager or DomainCircuitBreakerManager()
        self._telemetry = telemetry
        self._adapters = request_adapters or [TimeoutAdapter()]

    async def execute(
        self,
        executor: Executor,
        *,
        method: str,
        url: str,
        domain: str,
        **kwargs: Any,
    ) -> Any:
        retry_count = 0
        retry_state = RetryState(method=method, url=url, original_kwargs=kwargs.copy())

        while True:
            try:
                return await self._circuit_breakers.execute(
                    domain, executor, method=method, url=url, **kwargs
                )
            except BaseException as exc:
                response = getattr(exc, "response", None)
                category = ErrorClassifier.categorize(exc, response)

                if self._telemetry is not None:
                    try:
                        await self._telemetry.record_error(domain, category, response)
                    except Exception:  # never let telemetry break the request
                        logger.debug("ErrorTelemetry.record_error failed", exc_info=True)

                should_retry, backoff = self._policy.should_retry(exc, response, retry_count)

                retry_state.attempts.append(
                    {
                        "timestamp": time.monotonic(),
                        "exception": repr(exc),
                        "category": category,
                        "backoff": backoff if should_retry else 0.0,
                        "response": response,
                    }
                )

                if not should_retry:
                    if retry_count == 0:
                        # Never retried — caller wants the original typed exception.
                        raise
                    raise RetryError(
                        f"Request failed after {retry_count} retries: {exc}",
                        original_exception=exc,
                        retry_state=retry_state,
                    ) from exc

                logger.info(
                    "Retrying %s %s after %s, retry %d in %.2fs",
                    method,
                    _safe_url_for_log(url),
                    category,
                    retry_count + 1,
                    backoff,
                )
                await asyncio.sleep(backoff)
                retry_count += 1
                kwargs = await self._prepare_retry(retry_state, kwargs)

    async def _prepare_retry(
        self, retry_state: RetryState, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        modified = kwargs.copy()
        for adapter in self._adapters:
            modified = await adapter.adapt_request(retry_state, modified)
        return modified
