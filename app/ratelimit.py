"""Rate limiter implementations behind the RateLimiter protocol (app/interfaces.py).

Default is in-process (per-worker). For a hard global cap across workers, provide a
shared implementation (e.g. Redis) that satisfies the same protocol and select it via
CAREFIND_RATE_LIMITER (see build_rate_limiter); no call-site change needed.
"""
import time
from collections import defaultdict, deque

from .config import settings
from .interfaces import RateDecision


class InProcessRateLimiter:
    """Fixed-window per-client limiter, in process. Reads the max/window from settings
    live so they can be reconfigured at runtime. Idle buckets are swept so memory can't
    grow unbounded across many distinct client keys.

    `now` is injectable purely so tests can drive the clock; production uses the
    monotonic clock.
    """

    def __init__(self, *, now=time.monotonic):
        self._hits: dict = defaultdict(deque)
        self._last_sweep = 0.0
        self._now = now

    def reset(self) -> None:
        self._hits.clear()
        self._last_sweep = 0.0

    def check(self, key: str) -> RateDecision:
        max_ = settings.rate_limit_max
        window = settings.rate_limit_window
        if not max_:                       # 0 disables limiting entirely
            return RateDecision(True)
        now = self._now()
        if now - self._last_sweep > window:
            for stale in [k for k, d in self._hits.items() if not d or now - d[-1] > window]:
                del self._hits[stale]
            self._last_sweep = now
        dq = self._hits[key]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= max_:
            return RateDecision(False, retry_after=window)
        dq.append(now)
        return RateDecision(True)


class NoopRateLimiter:
    """Never limits. Useful when an external gateway already enforces limits, or in
    tests. Also the shape a shared/no-op backend swap must satisfy."""

    def check(self, key: str) -> RateDecision:
        return RateDecision(True)


def build_rate_limiter():
    """Select the rate limiter from config. Defaults to in-process."""
    kind = settings.rate_limiter
    if kind == "noop":
        return NoopRateLimiter()
    return InProcessRateLimiter()
