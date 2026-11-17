"""In-memory rate limits: the public site's waitlist and the API's per-user limits.

Imported by the site app too, so it must not import the API's routes or UI.
"""

import time
from collections import deque


class SlidingWindowLimiter:
    """At most `limit` events per `window` seconds per key, in memory.

    One process is one limiter: behind several workers it is per worker, which is still a bound. `max_keys` caps memory: when full, the
    oldest keys are dropped, which only ever lets a long-quiet address through again.
    """

    def __init__(self, limit: int, window: float, max_keys: int = 10_000, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        events = self._events.setdefault(key, deque())
        while events and now - events[0] > self.window:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        if len(self._events) > self.max_keys:
            for stale in list(self._events)[: len(self._events) - self.max_keys]:
                self._events.pop(stale, None)
        return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until `key` may act again (0 if it may now)."""
        events = self._events.get(key)
        if not events or len(events) < self.limit:
            return 0
        return max(1, int(self.window - (self._clock() - events[0])) + 1)
