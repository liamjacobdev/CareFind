"""Geocoding source chain: Census primary, Nominatim fallback, SQLite cache."""
import httpx
import pytest
import respx

from app import db, geocode
from app.config import settings

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_REV = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REV = "https://nominatim.openstreetmap.org/reverse"


def _census_rev_hit(zip_code):
    return httpx.Response(200, json={"result": {"geographies": {
        "2020 Census ZIP Code Tabulation Areas": [{"ZCTA5": zip_code, "GEOID": zip_code}]}}})


def _census_hit(lat, lon):
    return httpx.Response(200, json={"result": {"addressMatches": [
        {"coordinates": {"x": lon, "y": lat}}]}})


def _census_miss():
    return httpx.Response(200, json={"result": {"addressMatches": []}})


@respx.mock
@pytest.mark.asyncio
async def test_census_match_populates_cache(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", True)
    route = respx.get(CENSUS).mock(return_value=_census_hit(30.76, -86.57))

    coords = await geocode.geocode_one("1 Main St, Crestview, FL 32536")
    assert coords == [30.76, -86.57]
    # Cached: a repeat lookup short-circuits without a second HTTP call.
    again = await geocode.geocode_one("1 Main St, Crestview, FL 32536")
    assert again == [30.76, -86.57]
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_census_miss_falls_through_to_nominatim(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", True)
    monkeypatch.setattr(settings, "geocode_min_interval", 0.0)  # no throttle wait in tests
    census = respx.get(CENSUS).mock(return_value=_census_miss())
    nomin = respx.get(NOMINATIM).mock(
        return_value=httpx.Response(200, json=[{"lat": "40.0", "lon": "-75.0"}])
    )

    coords = await geocode.geocode_one("999 Nowhere Rd, Somewhere, PA")
    assert coords == [40.0, -75.0]
    assert census.called and nomin.called


@respx.mock
@pytest.mark.asyncio
async def test_census_disabled_uses_nominatim_only(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", False)
    monkeypatch.setattr(settings, "geocode_min_interval", 0.0)
    census = respx.get(CENSUS).mock(return_value=_census_hit(1.0, 2.0))
    nomin = respx.get(NOMINATIM).mock(
        return_value=httpx.Response(200, json=[{"lat": "12.0", "lon": "-34.0"}])
    )

    coords = await geocode.geocode_one("1 Main St, Crestview, FL")
    assert coords == [12.0, -34.0]
    assert not census.called and nomin.called


@respx.mock
@pytest.mark.asyncio
async def test_batch_uses_cache_and_census(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", True)
    # Pre-warm the cache for one address; it must not trigger an HTTP call.
    db.geocode_set(geocode._key("1 Cached St, Town, FL"), 10.0, 20.0)
    route = respx.get(CENSUS).mock(return_value=_census_hit(30.0, -86.0))

    out = await geocode.geocode_batch([
        {"key": "cached", "q": "1 Cached St, Town, FL"},
        {"key": "live", "q": "2 Live Ave, Town, FL"},
    ])
    assert out["cached"] == [10.0, 20.0]
    assert out["live"] == [30.0, -86.0]
    assert route.call_count == 1  # only the uncached address hit the network


# ── Reverse geocoding (coords -> ZIP, for 'Near me') ──────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_reverse_census_resolves_zip_and_caches(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", True)
    route = respx.get(CENSUS_REV).mock(return_value=_census_rev_hit("20006"))

    zip_code = await geocode.reverse(38.8977, -77.0365)
    assert zip_code == "20006"
    # Warm tap: served from revcache with no second HTTP call.
    again = await geocode.reverse(38.8977, -77.0365)
    assert again == "20006"
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_reverse_falls_back_to_nominatim(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", True)
    monkeypatch.setattr(settings, "geocode_min_interval", 0.0)
    census = respx.get(CENSUS_REV).mock(return_value=httpx.Response(500))
    nomin = respx.get(NOMINATIM_REV).mock(
        return_value=httpx.Response(200, json={"address": {"postcode": "02134"}})
    )

    zip_code = await geocode.reverse(42.0, -71.0)
    assert zip_code == "02134"
    assert census.called and nomin.called


@respx.mock
@pytest.mark.asyncio
async def test_reverse_transient_failure_not_cached(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "geocode_use_census", True)
    monkeypatch.setattr(settings, "geocode_min_interval", 0.0)
    census = respx.get(CENSUS_REV).mock(return_value=httpx.Response(500))
    nomin = respx.get(NOMINATIM_REV).mock(return_value=httpx.Response(500))

    assert await geocode.reverse(10.0, 20.0) == ""
    # Both sources failed -> nothing cached, so a retry hits the network again.
    census.reset()
    await geocode.reverse(10.0, 20.0)
    assert census.called
