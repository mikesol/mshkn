from __future__ import annotations

from mshkn.ratelimit import RateLimiter


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_n_then_rejects_until_the_window_slides() -> None:
    clock = Clock()
    limiter = RateLimiter(max_requests=3, window_seconds=10.0, clock=clock)
    accepted = []
    for _ in range(3):  # t = 1000.0, 1001.0, 1002.0
        accepted.append(limiter.check("k"))
        clock.now += 1.0
    assert accepted == [True, True, True]
    assert limiter.check("k") is False  # t = 1003.0, all three still in the window
    clock.now += 6.9
    assert limiter.check("k") is False  # t = 1009.9, the oldest is 9.9 s old
    clock.now += 0.2
    assert limiter.check("k") is True  # t = 1010.1, the oldest has aged out
    assert limiter.check("k") is False  # and the window is full again


def test_keys_are_independent() -> None:
    limiter = RateLimiter(max_requests=1, window_seconds=60.0, clock=Clock())
    assert limiter.check("a") is True
    assert limiter.check("b") is True
    assert limiter.check("a") is False
    assert limiter.check("b") is False


def test_rejected_requests_do_not_extend_the_window() -> None:
    clock = Clock()
    limiter = RateLimiter(max_requests=1, window_seconds=10.0, clock=clock)
    assert limiter.check("k") is True
    for _ in range(5):
        clock.now += 1.0
        assert limiter.check("k") is False
    clock.now += 5.1  # t = 1010.1: 10.1 s after the one accepted request
    assert limiter.check("k") is True


def test_the_default_clock_is_a_real_one() -> None:
    """Covers the default-argument path; any working clock satisfies this."""
    limiter = RateLimiter(max_requests=1, window_seconds=10.0)
    assert limiter.check("k") is True
    assert limiter.check("k") is False
