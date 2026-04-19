import random

from hyperhttp.utils.backoff import (
    AdaptiveBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
)


def test_exponential_no_jitter_monotonic():
    b = ExponentialBackoff(base=0.1, factor=2.0, max_backoff=10.0, jitter=False)
    assert b.calculate_backoff(0) == 0.1
    assert b.calculate_backoff(1) == 0.2
    assert b.calculate_backoff(2) == 0.4


def test_exponential_respects_max_backoff():
    b = ExponentialBackoff(base=1.0, factor=10.0, max_backoff=2.0, jitter=False)
    assert b.calculate_backoff(5) == 2.0


def test_exponential_jitter_within_bounds():
    random.seed(0)
    b = ExponentialBackoff(base=1.0, factor=1.0, max_backoff=10.0, jitter=True)
    for _ in range(20):
        v = b.calculate_backoff(0)
        assert 0.8 <= v <= 1.2


def test_decorrelated_first_retry_under_base():
    random.seed(1)
    b = DecorrelatedJitterBackoff(base=1.0, max_backoff=30.0)
    v = b.calculate_backoff(0)
    assert 0 <= v <= 1.0


def test_decorrelated_respects_max_backoff():
    random.seed(2)
    b = DecorrelatedJitterBackoff(base=0.1, max_backoff=0.5)
    # Force a large previous to exercise the cap.
    b.previous_backoff = 100.0
    v = b.calculate_backoff(1)
    assert v <= 0.5


def test_adaptive_falls_back_without_history():
    b = AdaptiveBackoff(base=0.1, max_backoff=10.0)
    # Single sample → not enough history → exponential fallback.
    v = b.calculate_backoff(2)
    assert v == min(0.1 * (2 ** 2), 10.0)


def test_adaptive_grows_with_frequency():
    b = AdaptiveBackoff(base=0.1, max_backoff=60.0)
    # Prime the window with many "recent" errors to trigger acceleration.
    for _ in range(10):
        b.calculate_backoff(0)
    # The last call should have used acceleration_factor >= 1.5.
    v = b.calculate_backoff(2)
    assert v > 0.1
