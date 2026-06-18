"""Tiny in-process metrics counters, scraped by GET /metrics.

Deliberately dependency-free and per-process (like the rate limiter) — enough to see
request volume, cache effectiveness, and upstream health without standing up a metrics
backend. For a multi-worker deployment, front with a real aggregator; the hit-rate
signals still hold per worker.
"""
import threading
from collections import Counter
from typing import Any

_lock = threading.Lock()
_counts: Counter[str] = Counter()


def incr(name: str, n: int = 1) -> None:
    with _lock:
        _counts[name] += n


def _rate(hits: int, misses: int) -> float | None:
    total = hits + misses
    return round(hits / total, 4) if total else None


def snapshot() -> dict[str, Any]:
    with _lock:
        d = dict(_counts)
    g_hit, g_miss = d.get("geocode_hit", 0), d.get("geocode_miss", 0)
    f_hit, f_miss = d.get("fhir_hit", 0), d.get("fhir_miss", 0)
    n_hit, n_miss = d.get("nppes_hit", 0), d.get("nppes_miss", 0)
    return {
        "requests_total": d.get("requests_total", 0),
        "requests_by_status": {
            k.split(":", 1)[1]: v for k, v in sorted(d.items()) if k.startswith("status:")
        },
        "geocode_cache": {"hits": g_hit, "misses": g_miss, "hit_rate": _rate(g_hit, g_miss)},
        "fhir_cache": {"hits": f_hit, "misses": f_miss, "hit_rate": _rate(f_hit, f_miss)},
        "nppes_cache": {"hits": n_hit, "misses": n_miss, "hit_rate": _rate(n_hit, n_miss)},
        "upstream_errors": d.get("upstream_error", 0),
        "circuits_opened": d.get("circuit_open", 0),
    }


def reset() -> None:
    """Clear all counters (used by tests for isolation)."""
    with _lock:
        _counts.clear()
