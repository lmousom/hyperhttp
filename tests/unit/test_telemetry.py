import asyncio

import pytest

from hyperhttp.errors.telemetry import ErrorTelemetry


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


pytestmark = pytest.mark.asyncio


async def test_record_error_creates_domain_stats():
    t = ErrorTelemetry()
    await t.record_error("api.example.com", "TIMEOUT", _Resp(504))
    await t.record_error("api.example.com", "TIMEOUT")
    await t.record_error("api.example.com", "SERVER", _Resp(500))

    rate = await t.get_error_rate("api.example.com", window_seconds=60.0)
    assert rate > 0

    report = await t.get_domain_report("api.example.com")
    assert report is not None
    assert report["total_errors"] == 3
    assert report["error_categories"]["TIMEOUT"] == 2
    assert report["status_codes"] == {504: 1, 500: 1}
    assert len(report["recent_errors"]) == 3


async def test_error_rate_zero_for_unknown_domain():
    t = ErrorTelemetry()
    rate = await t.get_error_rate("unknown.example.com")
    assert rate == 0.0


async def test_domain_report_none_when_no_data():
    t = ErrorTelemetry()
    report = await t.get_domain_report("missing")
    assert report is None


async def test_get_all_domains_lists_recorded():
    t = ErrorTelemetry()
    await t.record_error("a.example", "TIMEOUT")
    await t.record_error("b.example", "SERVER")
    assert set(await t.get_all_domains()) == {"a.example", "b.example"}


async def test_global_stats_aggregates_across_domains():
    t = ErrorTelemetry()
    await t.record_error("a.example", "TIMEOUT", _Resp(504))
    await t.record_error("b.example", "SERVER", _Resp(500))
    await t.record_error("b.example", "TIMEOUT", _Resp(504))

    stats = await t.get_global_stats()
    assert stats["total_errors"] == 3
    assert stats["total_domains"] == 2
    assert stats["categories"]["TIMEOUT"] == 2
    assert stats["categories"]["SERVER"] == 1
    assert stats["status_codes"] == {504: 2, 500: 1}
    assert set(stats["domains_by_error"]["TIMEOUT"]) == {"a.example", "b.example"}


async def test_expiry_task_started_after_record():
    t = ErrorTelemetry()
    await t.record_error("x.example", "TIMEOUT")
    assert t._expiry_task is not None
    # Cancel so the test doesn't leak the background task.
    t._expiry_task.cancel()
    try:
        await t._expiry_task
    except asyncio.CancelledError:
        pass


async def test_perform_expiry_drops_inactive_domains():
    t = ErrorTelemetry()
    await t.record_error("stale.example", "TIMEOUT")
    # Rewrite timestamps to something ancient so the expiry loop drops them.
    t._domain_stats["stale.example"]["error_timestamps"].clear()
    t._domain_stats["stale.example"]["error_timestamps"].append(0.0)
    t._domain_stats["stale.example"]["total_errors"] = 1  # below retain threshold
    await t._perform_expiry()
    assert "stale.example" not in t._domain_stats
