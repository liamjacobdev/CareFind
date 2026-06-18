"""Server-side proxy to the official CMS NPPES registry API (v2.1).

The browser never calls NPPES directly when a backend is configured: this keeps
the public CORS-proxy fallbacks out of the hot path and lets us shape errors.
The query mapping mirrors the frontend's buildNpiParams() exactly so results are
identical whether the page runs standalone or backed by this API.
"""
import json
from typing import Any

import httpx

from . import metrics
from .cache import build_cache
from .circuit import CircuitBreaker
from .config import settings

# Trip after a few consecutive NPPES failures so an outage fast-fails instead of every
# request eating the full timeout; auto-recovers after the cooldown.
_breaker = CircuitBreaker("nppes")

# Short-TTL cache of NPPES search results (C4), keyed on the effective query params. An
# identical repeat search (common: pagination, a user re-running the same query, the
# providers/search + npi paths overlapping) is served from here, making zero live calls
# and sparing the public registry. Behind the B2 CacheBackend seam, so it can be shared
# across workers via Redis without touching this code.
_cache = build_cache()


def _wild(value: str) -> str:
    value = (value or "").strip().rstrip("*")
    return value + "*" if value else ""


def build_params(q: dict[str, Any]) -> dict[str, Any]:
    """Translate the frontend's search fields into NPPES query parameters.

    Raises ValueError when the query is too empty for NPPES to accept it.
    """
    npi = str(q.get("npi") or "").strip()

    limit = q.get("limit") or 25
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 25

    params: dict[str, Any] = {"version": "2.1", "limit": limit, "skip": 0}

    if npi:
        if not (npi.isdigit() and len(npi) == 10):
            raise ValueError("An NPI must be exactly 10 digits.")
        params["number"] = npi
        return params

    # Cap input lengths (D3): bound each field so a hostile or mistyped param can't
    # bloat the upstream query. Generous for real names/cities; tight where the field
    # is a code. No real value is truncated in practice.
    def _f(key: str, n: int) -> str:
        return str(q.get(key) or "").strip()[:n]

    zip_ = _f("zip", 10)
    city = _f("city", 80)
    state = _f("state", 2)
    name = _f("name", 80)
    taxonomy = _f("taxonomy", 80)
    etype = _f("type", 8)

    try:
        radius = int(q.get("radius") or 0)
    except (TypeError, ValueError):
        radius = 0

    if zip_:
        # Real radius: widen the candidate pool beyond the exact ZIP using a
        # postal-code prefix wildcard (NPPES supports trailing '*'), then the
        # caller distance-filters geocoded results. <=10mi stays an exact match;
        # wider searches use the 3-digit ZIP prefix (~a regional cluster).
        if radius > 10 and len(zip_) == 5:
            params["postal_code"] = zip_[:3] + "*"
        else:
            params["postal_code"] = zip_
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if zip_ or city:
        params["address_purpose"] = "LOCATION"
    if taxonomy:
        params["taxonomy_description"] = taxonomy
    if etype in ("NPI-1", "NPI-2"):
        params["enumeration_type"] = etype
    if name:
        if etype == "NPI-2":
            params["organization_name"] = _wild(name)
        else:
            parts = name.split()
            if len(parts) > 1:
                params["first_name"] = _wild(parts[0])
                params["last_name"] = _wild(" ".join(parts[1:]))
            else:
                params["last_name"] = _wild(name)

    searchable = ("postal_code", "city", "state", "organization_name",
                  "first_name", "last_name", "taxonomy_description")
    if not any(k in params for k in searchable):
        raise ValueError(
            "Provide a ZIP code, a city and state, an NPI, or a name to search."
        )
    return params


async def search(q: dict[str, Any]) -> list[Any]:
    """Return the raw NPPES `results` list (the frontend/normalizer consumes it).

    Cached on the effective query params with a short TTL: an identical repeat search is
    served without a live call. A bad query raises before the cache (build_params), and
    only successful results are cached — a transient upstream failure is never pinned."""
    params = build_params(q)
    key = "nppes:" + json.dumps(params, sort_keys=True)
    cached = _cache.get(key)
    if isinstance(cached, list):
        metrics.incr("nppes_hit")
        return cached
    metrics.incr("nppes_miss")
    results = await _live_search(params)
    _cache.set(key, results, settings.nppes_cache_ttl)
    return results


async def _live_search(params: dict[str, Any]) -> list[Any]:
    """One NPPES query (with a single retry); raises on a bad query or unreachable upstream.
    Guarded by a circuit breaker (D4): while NPPES is failing, fast-fail instead of eating
    a full timeout per request."""
    import asyncio

    if not _breaker.allow():
        raise RuntimeError("NPPES circuit open — upstream is failing; not attempting.")

    headers = {"Accept": "application/json", "User-Agent": settings.contact_ua}
    last_exc = None
    async with httpx.AsyncClient(timeout=18) as client:
        # One quick retry: the public registry occasionally throttles or drops a
        # connection on rapid repeat queries; a single retry smooths that over.
        for attempt in range(2):
            try:
                resp = await client.get(settings.nppes_base, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    await asyncio.sleep(0.8)
        else:
            assert last_exc is not None
            _breaker.record_failure()
            raise last_exc

    _breaker.record_success()
    if isinstance(data, dict) and data.get("Errors"):
        # NPPES reports bad queries in an Errors array — surface as a 400. (A bad query
        # is a client error, not an upstream failure, so the breaker already saw success.)
        raise ValueError(data["Errors"][0].get("description", "The registry rejected the query."))
    out = data.get("results", []) if isinstance(data, dict) else []
    return out if isinstance(out, list) else []
