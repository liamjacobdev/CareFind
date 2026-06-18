"""A small circuit breaker for upstream calls (D4).

Every upstream (NPPES, the geocoders, each FHIR Plan-Net directory) is already bounded
(timeouts) and degrades safely on error. The breaker adds the missing piece: when an
upstream is *down*, stop hammering it — after `fail_max` consecutive failures the breaker
**opens** and calls fast-fail for `reset_timeout` seconds instead of each one eating a
full timeout. After the cooldown it goes **half-open** and lets a trial through; success
closes it, failure re-opens it. This bounds latency and load during an outage and lets a
recovered upstream heal automatically.

Thread-safe and clock-injectable (for tests). The caller owns the degrade behavior:

    if not breaker.allow():
        return <degraded>            # fast-fail; don't touch the upstream
    try:
        result = await call_upstream()
        breaker.record_success()
        return result
    except Exception:
        breaker.record_failure()
        ...                          # degrade (return unknown / raise a 502)
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from . import metrics

log = logging.getLogger("carefind.circuit")


class CircuitBreaker:
    def __init__(self, name: str, *, fail_max: int = 5, reset_timeout: float = 30.0,
                 now: Callable[[], float] | None = None) -> None:
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        import time
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"  # "closed" | "open" | "half-open"

    @property
    def state(self) -> str:
        # Resolve a due cooldown so reads reflect the half-open transition.
        with self._lock:
            self._refresh_locked()
            return self._state

    def _refresh_locked(self) -> None:
        if self._state == "open" and (self._now() - self._opened_at) >= self.reset_timeout:
            self._state = "half-open"

    def allow(self) -> bool:
        """True if a call may proceed. Open -> False until the cooldown elapses, then a
        half-open trial is permitted."""
        with self._lock:
            self._refresh_locked()
            return self._state != "open"

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state != "closed":
                log.info("circuit %s -> closed (recovered)", self.name)
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state != "open" and (self._state == "half-open" or self._failures >= self.fail_max):
                self._state = "open"
                self._opened_at = self._now()
                metrics.incr("circuit_open")
                log.warning("circuit %s -> OPEN after %d failures; fast-failing for %.0fs",
                            self.name, self._failures, self.reset_timeout)

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._state = "closed"
