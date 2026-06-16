"""Cache implementations behind the CacheBackend protocol (app/interfaces.py).

Default is an in-process TTL cache (per-worker). For shared warmth across workers,
provide a Redis-backed implementation satisfying the same protocol and select it via
CAREFIND_CACHE (see build_cache). Used by the short-TTL NPPES result cache (C4).
"""
import threading
import time
from collections.abc import Callable
from typing import Any

from .config import settings
from .interfaces import CacheBackend


class InProcessTTLCache:
    """A tiny thread-safe TTL cache. Entries expire lazily on read and are also swept
    opportunistically once the store grows past a soft cap, so a long-lived process
    can't accumulate stale keys without bound."""

    def __init__(self, *, max_entries: int = 2048, now: Callable[[], float] = time.monotonic) -> None:
        self._store: dict[str, tuple[float, Any]] = {}   # key -> (expires_at, value)
        self._lock = threading.Lock()
        self._max = max_entries
        self._now = now

    def get(self, key: str) -> Any:
        now = self._now()
        with self._lock:
            row = self._store.get(key)
            if row is None:
                return None
            expires_at, value = row
            if expires_at <= now:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        now = self._now()
        with self._lock:
            if len(self._store) >= self._max:
                # Drop everything already expired; if still full, this is a soft cap so
                # we evict the oldest-expiring entries to make room.
                dead = [k for k, (exp, _) in self._store.items() if exp <= now]
                for k in dead:
                    self._store.pop(k, None)
                if len(self._store) >= self._max:
                    for k, _ in sorted(self._store.items(), key=lambda kv: kv[1][0])[: self._max // 4 or 1]:
                        self._store.pop(k, None)
            self._store[key] = (now + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class NoopCache:
    """Caches nothing — every get is a miss. The shape a no-op swap must satisfy."""

    def get(self, key: str) -> Any:
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        pass


def build_cache() -> CacheBackend:
    """Select the cache backend from config. Defaults to the in-process TTL cache."""
    kind = settings.cache_backend
    if kind == "noop":
        return NoopCache()
    return InProcessTTLCache()
