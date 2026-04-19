import datetime
import email.utils
import time

import pytest

from hyperhttp._headers import Headers
from hyperhttp.errors.retry import (
    RequestAdapter,
    RetryError,
    RetryHandler,
    RetryPolicy,
    RetryState,
    TimeoutAdapter,
)
from hyperhttp.exceptions import ReadTimeout


class _Resp:
    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self.headers = Headers({"retry-after": retry_after} if retry_after else {})


def test_should_not_retry_past_max():
    p = RetryPolicy(max_retries=2)
    ok, backoff = p.should_retry(ReadTimeout("x"), response=None, retry_count=2)
    assert ok is False
    assert backoff == 0.0


def test_should_retry_transient_category():
    p = RetryPolicy(max_retries=3)
    ok, backoff = p.should_retry(ReadTimeout("x"), response=None, retry_count=0)
    assert ok is True
    assert backoff >= 0


def test_should_not_retry_unclassified_category():
    p = RetryPolicy(max_retries=3, retry_categories=["SERVER"])
    ok, _ = p.should_retry(ReadTimeout("x"), response=None, retry_count=0)
    assert ok is False


def test_retry_after_numeric():
    p = RetryPolicy(max_retries=3)
    resp = _Resp(503, retry_after="2")
    ok, backoff = p.should_retry(Exception("boom"), response=resp, retry_count=0)
    assert ok is True
    assert backoff == 2.0


def test_retry_after_http_date_future():
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
    header = email.utils.format_datetime(future)
    p = RetryPolicy(max_retries=3)
    resp = _Resp(503, retry_after=header)
    ok, backoff = p.should_retry(Exception("boom"), response=resp, retry_count=0)
    assert ok is True
    assert backoff >= 0.0


def test_retry_after_garbage_falls_back_to_backoff():
    p = RetryPolicy(max_retries=3)
    resp = _Resp(503, retry_after="not-a-date")
    ok, backoff = p.should_retry(Exception("boom"), response=resp, retry_count=0)
    assert ok is True
    # Either the parsed value was None and we fell through to the strategy…
    assert backoff >= 0


def test_status_not_in_force_list_skipped_when_category_unretryable():
    p = RetryPolicy(max_retries=3, status_force_list=[503])
    resp = _Resp(404)  # CLIENT → not retryable
    ok, _ = p.should_retry(Exception("boom"), response=resp, retry_count=0)
    assert ok is False


@pytest.mark.asyncio
async def test_timeout_adapter_bumps_timeout_after_timeout_failure():
    state = RetryState(method="GET", url="http://x/", original_kwargs={"timeout": 10.0})
    state.attempts.append({"category": "TIMEOUT"})
    adapter = TimeoutAdapter()
    kwargs = await adapter.adapt_request(state, {"timeout": 10.0})
    assert kwargs["timeout"] == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_timeout_adapter_caps_at_120():
    state = RetryState(method="GET", url="http://x/", original_kwargs={})
    state.attempts.append({"category": "TIMEOUT"})
    adapter = TimeoutAdapter()
    kwargs = await adapter.adapt_request(state, {"timeout": 200.0})
    assert kwargs["timeout"] == 120.0


@pytest.mark.asyncio
async def test_timeout_adapter_noop_without_timeout_error():
    state = RetryState(method="GET", url="http://x/", original_kwargs={})
    state.attempts.append({"category": "SERVER"})
    adapter = TimeoutAdapter()
    kwargs = await adapter.adapt_request(state, {"timeout": 10.0})
    assert kwargs["timeout"] == 10.0


@pytest.mark.asyncio
async def test_timeout_adapter_noop_on_first_attempt():
    state = RetryState(method="GET", url="http://x/", original_kwargs={})
    adapter = TimeoutAdapter()
    kwargs = await adapter.adapt_request(state, {"timeout": 10.0})
    assert kwargs == {"timeout": 10.0}


@pytest.mark.asyncio
async def test_retry_state_accessors():
    state = RetryState(method="GET", url="http://x/", original_kwargs={})
    assert state.attempt_count == 0
    assert state.last_error_category is None
    assert state.total_delay == 0.0
    assert state.elapsed >= 0
    state.attempts.append({"category": "TIMEOUT", "backoff": 0.5})
    state.attempts.append({"category": "SERVER", "backoff": 1.0})
    assert state.attempt_count == 2
    assert state.last_error_category == "SERVER"
    assert state.total_delay == 1.5


@pytest.mark.asyncio
async def test_retry_handler_returns_original_exception_after_zero_retries():
    p = RetryPolicy(max_retries=0)
    handler = RetryHandler(retry_policy=p)

    async def boom(**_kwargs):
        raise ReadTimeout("first")

    with pytest.raises(ReadTimeout, match="first"):
        await handler.execute(boom, method="GET", url="http://x/", domain="x")


@pytest.mark.asyncio
async def test_retry_handler_succeeds_after_retry(monkeypatch):
    # Skip the real sleep to keep the test fast.
    import hyperhttp.errors.retry as retry_mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(retry_mod.asyncio, "sleep", _no_sleep)

    p = RetryPolicy(max_retries=2)
    handler = RetryHandler(retry_policy=p)

    state = {"calls": 0}

    async def flaky(**_kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise ReadTimeout("transient")
        return "ok"

    result = await handler.execute(flaky, method="GET", url="http://x/", domain="x")
    assert result == "ok"
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_retry_handler_wraps_in_retry_error_after_retries(monkeypatch):
    import hyperhttp.errors.retry as retry_mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(retry_mod.asyncio, "sleep", _no_sleep)

    p = RetryPolicy(max_retries=2)
    handler = RetryHandler(retry_policy=p)

    async def always_fail(**_kwargs):
        raise ReadTimeout("broken")

    with pytest.raises(RetryError) as excinfo:
        await handler.execute(always_fail, method="GET", url="http://x/", domain="x")
    assert isinstance(excinfo.value.original_exception, ReadTimeout)
    assert excinfo.value.retry_state.attempt_count == 3
