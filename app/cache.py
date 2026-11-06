"""A tiny in-process TTL cache for hot read endpoints.

The stats it serves only change every 15 min (the estimator's cadence), so caching for a
minute saves repeated percentile queries without serving anything meaningfully stale.
shortcut: per-process memory, so N API workers keep N copies; use Redis if we ever run
several API replicas and need them to agree.
"""

import time
from collections.abc import Callable, Hashable
from threading import Lock


class TTLCache:
    def __init__(self, ttl_seconds: float, maxsize: int = 1024, clock=time.monotonic) -> None:
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self.clock = clock
        self._data: dict[Hashable, tuple[float, object]] = {}
        self._lock = Lock()

    def get_or_set(self, key: Hashable, compute: Callable[[], object]) -> object:
        now = self.clock()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = compute()  # outside the lock: a slow query mustn't block other keys
        with self._lock:
            if len(self._data) >= self.maxsize:
                self._data.clear()  # crude but bounded; entries are cheap to rebuild
            self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
