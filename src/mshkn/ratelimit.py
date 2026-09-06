from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class RateLimiter:
    """Per-key sliding window rate limiter (in-memory).

    ``clock`` is the monotonic time source. Tests inject one they advance by
    hand so the window can be exercised without sleeping.
    """

    def __init__(
        self,
        max_requests: int = 50,
        window_seconds: float = 10.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = self._clock()
        cutoff = now - self.window_seconds

        with self._lock:
            timestamps = self._timestamps.get(key, [])
            # Remove expired entries
            timestamps = [t for t in timestamps if t > cutoff]

            if len(timestamps) >= self.max_requests:
                self._timestamps[key] = timestamps
                return False

            timestamps.append(now)
            self._timestamps[key] = timestamps
            return True
