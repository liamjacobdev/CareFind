"""Geocoding with a free, out-of-the-box default and a persistent SQLite cache.

Source chain (server-side):
  1. US Census Geocoder — free, keyless, no contact header, US-only, no rate limit.
     This is the default so a fresh install has working map pins/distances with no
     configuration at all.
  2. OpenStreetMap Nominatim — fallback. Free but its usage policy requires a real
     contact email in the User-Agent (set CAREFIND_UA) and max ~1 req/sec, so it is
     politely throttled. Used only when Census misses or is disabled.

A single browser request to /api/providers/search geocodes a whole page of results
here: cache hits are free; misses are resolved live (Census concurrently, Nominatim
serialized by the throttle) within an optional time budget so a slow geocoder can
never block the caller.
"""
import asyncio
import logging
import math
import time

import httpx

from . import db, metrics
from .config import settings

log = logging.getLogger("carefind.geocode")

_rate_lock = asyncio.Lock()
_last_call = 0.0


def haversine_miles(a, b) -> float:
    """Great-circle distance in miles between [lat, lon] pairs."""
    r = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    d = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(d))


def _key(q: str) -> str:
    return " ".join((q or "").lower().split())


def census_enabled() -> bool:
    return settings.geocode_use_census


def active_geocoder() -> str:
    """Human-readable name of the primary geocoder, for startup logging."""
    return "US Census (primary), Nominatim (fallback)" if census_enabled() else "Nominatim"


# ── US Census Geocoder (primary; free, keyless, no contact header, US-only) ──
async def _census_search(client: httpx.AsyncClient, q: str):
    resp = await client.get(
        settings.census_base + "/geocoder/locations/onelineaddress",
        params={"benchmark": "Public_AR_Current", "format": "json", "address": q},
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    matches = (((data or {}).get("result") or {}).get("addressMatches")) or []
    if matches:
        c = matches[0].get("coordinates") or {}
        x, y = c.get("x"), c.get("y")  # x = longitude, y = latitude
        if x is not None and y is not None:
            return [float(y), float(x)]
    return None


# ── OpenStreetMap Nominatim (fallback; throttled per their usage policy) ──
async def _throttle() -> None:
    """Block until at least geocode_min_interval has passed since the last Nominatim
    call. Census has no such limit, so only the Nominatim path goes through here."""
    global _last_call
    async with _rate_lock:
        wait = settings.geocode_min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


async def _nominatim_search(client: httpx.AsyncClient, q: str):
    await _throttle()
    resp = await client.get(
        settings.nominatim_base + "/search",
        params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
        headers={"Accept": "application/json", "User-Agent": settings.contact_ua},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data:
        return [float(data[0]["lat"]), float(data[0]["lon"])]
    return None


async def _geocode_live(client: httpx.AsyncClient, q: str):
    """Resolve one address through the source chain: Census first, Nominatim on a
    miss/error. Returns [lat, lon] or None — never a fabricated coordinate."""
    if census_enabled():
        try:
            coords = await _census_search(client, q)
            if coords:
                return coords
        except Exception as e:
            log.warning("Census geocode failed for %r, trying Nominatim: %s: %s",
                        q, type(e).__name__, e)  # fall through to Nominatim
    try:
        return await _nominatim_search(client, q)
    except Exception as e:
        metrics.incr("upstream_error")
        log.warning("Nominatim geocode failed for %r: %s: %s", q, type(e).__name__, e)
        return None


async def geocode_one(q: str):
    if not q or not q.strip():
        return None
    key = _key(q)
    cached = db.geocode_get(key)
    if cached is not None:
        metrics.incr("geocode_hit")
        return cached
    metrics.incr("geocode_miss")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            coords = await _geocode_live(client, q)
    except Exception as e:
        log.warning("Geocode client error for %r: %s: %s", q, type(e).__name__, e)
        return None
    if coords:
        db.geocode_set(key, coords[0], coords[1])
    return coords


async def geocode_batch(items: list, budget_seconds: float = None) -> dict:
    """items: [{"key": str, "q": str}] -> {key: [lat, lon]} for everything found.

    Cache hits are always returned. Misses are resolved live until `budget_seconds`
    elapses (None = no limit), so a slow/unreachable geocoder can never block the
    caller past its budget; whatever isn't resolved stays a cache miss and is picked
    up on a later call, progressively warming the cache. Census misses are resolved
    concurrently (it has no rate limit); the Nominatim fallback is serialized by its
    own throttle regardless of the concurrency here.
    """
    out: dict = {}
    pending = []
    for item in items or []:
        k, q = item.get("key"), item.get("q")
        if not k or not q:
            continue
        cached = db.geocode_get(_key(q))
        if cached is not None:
            out[k] = cached
            metrics.incr("geocode_hit")
        else:
            pending.append((k, q))

    metrics.incr("geocode_miss", len(pending))
    if not pending:
        return out

    start = time.monotonic()
    sem = asyncio.Semaphore(8)

    async def one(client, k, q):
        async with sem:
            if budget_seconds is not None and (time.monotonic() - start) >= budget_seconds:
                return k, None
            coords = await _geocode_live(client, q)
            if coords:
                db.geocode_set(_key(q), coords[0], coords[1])
            return k, coords

    async with httpx.AsyncClient(timeout=12) as client:
        results = await asyncio.gather(
            *[one(client, k, q) for k, q in pending], return_exceptions=True
        )
    for res in results:
        if isinstance(res, Exception):
            continue
        k, coords = res
        if coords:
            out[k] = coords
    return out


# ── Reverse geocoding (coords -> ZIP, for 'Near me') ──────────────────────────
def _rev_key(lat, lon) -> str:
    """Cache key for a coordinate. Rounded to ~11 m so the cache survives the tiny
    jitter between successive browser geolocation fixes; ZIP areas are far larger."""
    return f"{float(lat):.4f},{float(lon):.4f}"


async def _census_reverse(client: httpx.AsyncClient, lat, lon) -> str:
    """Keyless ZIP lookup via the Census geographies/coordinates benchmark — mirrors
    the forward Census path so 'Near me' works out of the box with no CAREFIND_UA.
    The ZIP Code Tabulation Area (ZCTA5) is the ZIP for this point."""
    resp = await client.get(
        settings.census_base + "/geocoder/geographies/coordinates",
        params={
            "x": lon, "y": lat,  # x = longitude, y = latitude
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "all",
            "format": "json",
        },
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    geos = (((resp.json() or {}).get("result") or {}).get("geographies")) or {}
    for layer, rows in geos.items():
        if "zip" in layer.lower() or "zcta" in layer.lower():
            if rows:
                zc = rows[0].get("ZCTA5") or rows[0].get("GEOID") or ""
                if zc:
                    return str(zc)
    return ""


async def _nominatim_reverse(client: httpx.AsyncClient, lat, lon) -> str:
    await _throttle()
    resp = await client.get(
        settings.nominatim_base + "/reverse",
        params={"lat": lat, "lon": lon, "format": "json"},
        headers={"Accept": "application/json", "User-Agent": settings.contact_ua},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return (data.get("address") or {}).get("postcode", "") or ""
    return ""


async def reverse(lat, lon) -> str:
    """ZIP from coordinates (used by 'Near me'). Census first (keyless, works on a
    fresh install), Nominatim as a fallback. Cached in revcache so repeated 'Near me'
    taps don't re-hit the network. Returns '' when no ZIP can be resolved."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    key = _rev_key(lat_f, lon_f)
    cached = db.revgeocode_get(key)
    if cached is not None:
        return cached
    zip_code = ""
    async with httpx.AsyncClient(timeout=12) as client:
        if census_enabled():
            try:
                zip_code = await _census_reverse(client, lat_f, lon_f)
            except Exception as e:
                log.warning("Census reverse failed for (%s,%s), trying Nominatim: %s: %s",
                            lat_f, lon_f, type(e).__name__, e)  # fall through
        if not zip_code:
            try:
                zip_code = await _nominatim_reverse(client, lat_f, lon_f)
            except Exception as e:
                log.warning("Nominatim reverse failed for (%s,%s): %s: %s",
                            lat_f, lon_f, type(e).__name__, e)
    # Only cache a definite answer; a transient failure (both sources errored) stays
    # uncached so the next tap retries instead of pinning an empty result.
    if zip_code:
        db.revgeocode_set(key, zip_code)
    return zip_code
