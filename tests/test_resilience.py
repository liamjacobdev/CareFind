"""D4: circuit breakers on every upstream + readiness, so an outage fast-fails and
degrades safely (never a 500, never a fabricated answer) instead of piling up timeouts."""
import asyncio

import httpx
import pytest
import respx

from app import nppes
from app.circuit import CircuitBreaker
from app.config import settings
from app.insurance import FhirPlanNetSource


# ── CircuitBreaker unit ──────────────────────────────────────────────────────
def test_breaker_opens_fast_fails_and_recovers():
    clock = {"t": 0.0}
    cb = CircuitBreaker("t", fail_max=3, reset_timeout=30, now=lambda: clock["t"])
    assert cb.allow() and cb.state == "closed"

    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False          # fast-fail while open

    clock["t"] += 31                    # cooldown elapsed -> half-open trial allowed
    assert cb.allow() is True and cb.state == "half-open"

    cb.record_success()                 # trial succeeded -> closed
    assert cb.state == "closed" and cb.allow() is True


def test_breaker_reopens_if_half_open_trial_fails():
    clock = {"t": 0.0}
    cb = CircuitBreaker("t", fail_max=2, reset_timeout=10, now=lambda: clock["t"])
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    clock["t"] += 11
    assert cb.allow() and cb.state == "half-open"
    cb.record_failure()                 # trial failed -> straight back to open
    assert cb.state == "open" and cb.allow() is False


def test_a_clean_not_found_does_not_trip_the_breaker():
    cb = CircuitBreaker("t", fail_max=2)
    for _ in range(3):
        cb.record_success()
    assert cb.state == "closed"


# ── NPPES chaos: a down registry opens the breaker and fast-fails ─────────────
@respx.mock
@pytest.mark.asyncio
async def test_nppes_breaker_opens_and_stops_hammering(monkeypatch):
    monkeypatch.setattr(nppes, "_breaker", CircuitBreaker("nppes-test", fail_max=2))
    nppes._cache.clear()

    async def _nosleep(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _nosleep)  # don't actually wait on retries
    route = respx.get(settings.nppes_base).mock(return_value=httpx.Response(503))

    # Two failing searches trip the breaker (each search retries once = 2 calls).
    for i in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await nppes.search({"zip": f"3253{i}"})
    assert nppes._breaker.state == "open"
    calls_after_open = route.call_count

    # Further searches fast-fail WITHOUT touching the network.
    with pytest.raises(RuntimeError, match="circuit open"):
        await nppes.search({"zip": "90210"})
    assert route.call_count == calls_after_open  # zero additional live calls


# ── FHIR chaos: a down payer directory degrades to unknown, then skips calls ──
@respx.mock
@pytest.mark.asyncio
async def test_fhir_breaker_degrades_to_unknown_and_skips(temp_db):
    base = "https://down.example/r4"
    route = respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(503))
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})
    src._breaker = CircuitBreaker("demo", fail_max=3)

    first = await src.check_many(["1111111111", "2222222222", "3333333333"])
    assert all(v is None for v in first.values())   # degraded to unknown, never a yes
    assert src._breaker.state == "open"
    calls = route.call_count

    # New NPIs (not cached): breaker is open -> skipped, still unknown, no live calls.
    second = await src.check_many(["4444444444", "5555555555"])
    assert all(v is None for v in second.values())
    assert route.call_count == calls
