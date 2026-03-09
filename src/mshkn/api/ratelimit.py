from __future__ import annotations

import threading
import time


class RateLimiter:
    """Per-key sliding window rate limiter (in-memory)."""

    def __init__(self, max_requests: int = 50, window_seconds: float = 10.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
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


# Global rate limiter instance
rate_limiter = RateLimiter(max_requests=80, window_seconds=10.0)
