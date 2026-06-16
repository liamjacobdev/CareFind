"""Scale-readiness seams: the four external dependencies behind Protocols.

CareFind ships with $0 self-hosted defaults — SQLite, in-process cache, in-process
rate limiter, the keyless geocoders. Scaling to multiple workers or a bigger box is a
*config swap*, not a rewrite: provide an implementation that satisfies the relevant
Protocol (e.g. a Postgres `Datastore` or a Redis `RateLimiter`/`CacheBackend`) and
select it via the `build_*` factories. These Protocols are the contracts those
implementations must honor; conformance is enforced by tests (tests/test_interfaces.py)
and by mypy.

Protocols are structural and zero-cost: the existing concrete classes don't inherit
from them, they just match the shape. `@runtime_checkable` lets tests assert
conformance with `isinstance`.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class RateDecision:
    """The outcome of a RateLimiter.check(): whether the request is allowed and, if
    not, how many seconds the client should wait before retrying."""

    __slots__ = ("allowed", "retry_after")

    def __init__(self, allowed: bool, retry_after: int = 0) -> None:
        self.allowed = allowed
        self.retry_after = retry_after


@runtime_checkable
class RateLimiter(Protocol):
    """Per-client request throttling. In-process by default; a shared backend (e.g.
    Redis) gives a single global limit across workers (see D4)."""

    def check(self, key: str) -> RateDecision:
        """Record a hit for `key` and report whether it is allowed. Must be safe to
        call concurrently."""
        ...


@runtime_checkable
class CacheBackend(Protocol):
    """A small TTL cache (e.g. NPPES search results). In-process by default; a shared
    backend lets workers share warmth."""

    def get(self, key: str) -> Any:
        """Return the cached value for `key`, or None if absent/expired."""
        ...

    def set(self, key: str, value: Any, ttl: float) -> None:
        """Store `value` under `key` for `ttl` seconds."""
        ...


@runtime_checkable
class GeocoderBackend(Protocol):
    """Address/ZIP -> coordinates (and reverse). The default chains the keyless US
    Census geocoder and Nominatim with a SQLite cache."""

    async def geocode_one(self, q: str) -> list[float] | None: ...
    async def geocode_batch(
        self, items: list[dict[str, Any]], budget_seconds: float | None = None
    ) -> dict[str, list[float]]: ...
    async def reverse(self, lat: float, lon: float) -> str: ...
    def active_geocoder(self) -> str: ...


@runtime_checkable
class Datastore(Protocol):
    """Durable storage: the Medicare/TiC indexes, the FHIR + geocode caches, and
    source provenance. SQLite by default; a Postgres impl (D4) need only satisfy this
    same surface for multi-worker durability."""

    def init_db(self) -> None: ...

    # Medicare enrollment index
    def medicare_count(self) -> int: ...
    def medicare_has(self, npi: str) -> bool: ...
    def medicare_has_many(self, npis: list[str]) -> set[str]: ...
    def medicare_add_many(self, npis: list[str]) -> int: ...

    # Transparency-in-Coverage per-payer in-network index
    def tic_count(self, payer: str) -> int: ...
    def tic_has(self, payer: str, npi: str) -> bool: ...
    def tic_has_many(self, payer: str, npis: list[str]) -> set[str]: ...
    def tic_add_many(self, payer: str, npis: list[str]) -> int: ...

    # Geocode + reverse-geocode caches
    def geocode_get(self, key: str) -> list[float] | None: ...
    def geocode_set(self, key: str, lat: float, lon: float) -> None: ...
    def revgeocode_get(self, key: str) -> str | None: ...
    def revgeocode_set(self, key: str, postcode: str) -> None: ...

    # FHIR Plan-Net result cache
    def fhir_cache_get(self, payer: str, npi: str) -> tuple[str, float] | None: ...
    def fhir_cache_get_many(self, payer: str, npis: list[str]) -> dict[str, tuple[str, float]]: ...
    def fhir_cache_set(self, payer: str, npi: str, value: str, fetched_at: float) -> None: ...
    def fhir_cache_set_many(
        self, payer: str, items: list[tuple[str, str]], fetched_at: float
    ) -> int: ...

    # Source provenance (verify URL + fetch date)
    def source_meta_set(self, source_id: str, source_url: str, fetched_at: float) -> None: ...
    def source_meta_get(self, source_id: str) -> tuple[str, float] | None: ...
    def source_meta_all(self) -> dict[str, tuple[str, float]]: ...
