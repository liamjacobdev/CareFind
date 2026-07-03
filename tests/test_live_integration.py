"""Nightly live-integration (E1). These hit the REAL public services (NPPES, the
geocoders, the validated FHIR Plan-Net endpoints) and are skipped unless INNETWORK_LIVE=1,
so normal CI stays hermetic. The nightly workflow (.github/workflows/nightly.yml) sets
the flag, so a regression in a real upstream — or a payer endpoint that stops passing the
per-NPI round-trip — is caught within a day.
"""
import os

import httpx
import pytest

live = pytest.mark.skipif(
    not os.environ.get("INNETWORK_LIVE"),
    reason="live integration — set INNETWORK_LIVE=1 (nightly only)",
)


@live
@pytest.mark.asyncio
async def test_nppes_live_returns_results(temp_db):
    from app import nppes
    results = await nppes.search({"zip": "10001", "taxonomy": "Cardiovascular Disease", "limit": 5})
    assert isinstance(results, list) and results, "NPPES returned no results for a common query"
    assert results[0].get("number")


@live
@pytest.mark.asyncio
async def test_geocode_live_resolves_a_landmark(temp_db):
    from app import geocode
    coords = await geocode.geocode_one("350 5th Ave, New York, NY 10118")
    assert coords and len(coords) == 2
    assert 40 < coords[0] < 41 and -75 < coords[1] < -73  # roughly NYC


@live
def test_validated_planet_endpoints_still_pass_the_round_trip():
    """The endpoints we wire as verified must still pass the full per-NPI round-trip;
    if a payer changes its directory, this fails the nightly so we can demote it."""
    from app import planet_registry, verify_payers
    eps = planet_registry.validated()
    assert eps, "no validated endpoints to check"
    with httpx.Client() as c:
        for e in eps:
            res = verify_payers.probe(c, e)
            assert res["status"] == "validated", f"{e.id} regressed: {res.get('error')}"
