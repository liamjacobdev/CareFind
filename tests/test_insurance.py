"""The two-tier insurance model: confidence, regional gating, FHIR, merging."""
import httpx
import pytest
import respx

from app import db, insurance
from app.insurance import EstimatedPayerSource, FhirPlanNetSource, Registry


def _build():
    r = Registry()
    r.build()
    return r


def test_plans_grouped_by_category_with_confidence(temp_db):
    db.medicare_add_many(["1003000126"])
    reg = _build()
    cats = {c["id"]: c for c in reg.categories()}
    medicare = cats["medicare"]["plans"][0]
    assert medicare["id"] == "medicare" and medicare["confidence"] == "verified"
    # Commercial catalog payers are present as estimated.
    commercial_ids = {p["id"] for p in cats["commercial"]["plans"]}
    assert {"aetna", "cigna", "unitedhealthcare"} <= commercial_ids
    assert all(p["confidence"] == "estimated" for p in cats["commercial"]["plans"])


@pytest.mark.asyncio
async def test_medicare_verified_true_false(temp_db):
    db.medicare_add_many(["1003000126"])
    reg = _build()
    ann = await reg.annotate(
        [{"npi": "1003000126", "stateAb": "FL"}, {"npi": "9999999999", "stateAb": "FL"}],
        only=["medicare"],
    )
    assert ann["1003000126"]["medicare"] == {"value": True, "confidence": "verified", "source": "medicare"}
    assert ann["9999999999"]["medicare"]["value"] is False


@pytest.mark.asyncio
async def test_estimated_regional_gating(temp_db):
    reg = _build()
    ann = await reg.annotate(
        [{"npi": "1", "stateAb": "CA"}, {"npi": "2", "stateAb": "TX"}],
        only=["kaiser", "aetna"],
    )
    # Kaiser serves CA but not TX; Aetna is national.
    assert ann["1"]["kaiser"]["value"] is True and ann["1"]["kaiser"]["confidence"] == "estimated"
    assert "kaiser" not in ann["2"]
    assert ann["1"]["aetna"]["value"] is True and ann["2"]["aetna"]["value"] is True


@pytest.mark.asyncio
async def test_tic_ingest_after_build_surfaces_without_restart(temp_db, monkeypatch):
    """A TiC payer ingested AFTER the registry is built must surface as verified
    without a rebuild/restart — consistent with how Medicare already behaves."""
    # Bypass the short availability TTL so the post-ingest state is visible at once.
    monkeypatch.setattr(insurance, "_AVAILABILITY_TTL", 0.0)
    reg = _build()  # built with no TiC data

    before = await reg.annotate([{"npi": "1003000126", "stateAb": "CA"}], only=["aetna"])
    assert before["1003000126"]["aetna"]["confidence"] == "estimated"

    db.tic_add_many("aetna", ["1003000126"])  # ingest after build, no reg.build()

    after = await reg.annotate([{"npi": "1003000126", "stateAb": "CA"}], only=["aetna"])
    assert after["1003000126"]["aetna"]["confidence"] == "verified"


@pytest.mark.asyncio
async def test_verified_supersedes_estimate(temp_db):
    # A TiC ingest for aetna should make it verified and win over the estimate.
    db.tic_add_many("aetna", ["1003000126"])
    reg = _build()
    ann = await reg.annotate([{"npi": "1003000126", "stateAb": "CA"}], only=["aetna"])
    assert ann["1003000126"]["aetna"]["confidence"] == "verified"


@pytest.mark.asyncio
async def test_estimated_returns_true_or_none_never_false():
    national = EstimatedPayerSource({"id": "aetna", "label": "Aetna", "category": "commercial", "states": None})
    regional = EstimatedPayerSource({"id": "kaiser", "label": "Kaiser", "category": "commercial", "states": ["CA"]})
    # National serves everyone (even with no state); regional only its states.
    assert (await national.check_many_ctx({"1": {"state": ""}}))["1"] is True
    assert (await regional.check_many_ctx({"1": {"state": "CA"}}))["1"] is True
    assert (await regional.check_many_ctx({"1": {"state": "TX"}}))["1"] is None  # never False


@respx.mock
@pytest.mark.asyncio
async def test_fhir_check_many_concurrent_mapping():
    base = "https://payer.example/r4"
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})
    route = respx.get(f"{base}/PractitionerRole")

    def handler(request):
        ident = request.url.params.get("practitioner.identifier", "")
        if ident.endswith("|1111111111"):
            return httpx.Response(200, json={"entry": [
                {"resource": {"resourceType": "PractitionerRole", "active": True,
                              "network": [{"reference": "Organization/x"}]}}]})
        if ident.endswith("|2222222222"):
            return httpx.Response(200, json={"entry": []})  # found nothing -> False
        return httpx.Response(404)

    route.side_effect = handler
    out = await src.check_many(["1111111111", "2222222222", "3333333333"])
    assert out["1111111111"] is True
    assert out["2222222222"] is False
    assert out["3333333333"] is False  # 404 -> not in network
