"""Executable trust rules — the "never overclaim" invariant, enforced not just in prose.

CareFind's single most important promise (PLAN.md §1.1): a green "Confirmed"/verified
answer must be traceable to a real source for *that* provider; absence of data is
"unknown", never "no" and never a fabricated "yes". These tests encode that promise so
a regression fails CI instead of silently shipping a misleading badge.

They are deliberately exhaustive (every combination of the inputs that matter), which
gives property-test coverage without adding a dependency. CONTRIBUTING.md points here.
"""

import httpx
import pytest
import respx

from app import db, insurance
from app.config import settings
from app.insurance import (
    _FHIR_STR_TO_VALUE,
    EstimatedPayerSource,
    FhirPlanNetSource,
    Registry,
)

_IN_NETWORK = {"entry": [{"resource": {"resourceType": "PractitionerRole",
                                       "active": True, "network": [{"reference": "Network/x"}]}}]}


def _build():
    r = Registry()
    r.build()
    return r


# ── Rule 1: directory presence / unknown never becomes a Confirmed "yes" ───────
def _bundle(active, has_network, has_healthcare):
    res = {"resourceType": "PractitionerRole"}
    if active is not None:
        res["active"] = active
    if has_network:
        res["network"] = [{"reference": "Network/x"}]
    if has_healthcare:
        res["healthcareService"] = [{"reference": "HealthcareService/x"}]
    return {"entry": [{"resource": res}]}


@pytest.mark.parametrize("active", [True, False, None])
@pytest.mark.parametrize("has_network", [True, False])
@pytest.mark.parametrize("has_healthcare", [True, False])
@pytest.mark.parametrize("strictness", ["network", "directory"])
def test_in_network_is_true_only_when_provably_in_network(
    active, has_network, has_healthcare, strictness
):
    """Across every PractitionerRole shape, True is returned ONLY when an active role
    proves participation: a network link (always) or — in directory strictness — an
    active listing. Presence/healthcareService/inactive alone is never True."""
    result = FhirPlanNetSource._in_network(
        _bundle(active, has_network, has_healthcare), strictness=strictness)
    is_active = active is not False  # None or True both count as "not inactive"
    should_be_true = is_active and (has_network or strictness == "directory")
    if should_be_true:
        assert result is True
    else:
        assert result is not True  # unknown (None) or not-found (False), never a fake yes
    # healthcareService can never be the thing that flips an answer to True.
    if has_network is False and strictness == "network":
        assert result is not True


def test_empty_or_nonrole_bundle_is_never_true():
    for bundle in ({}, {"entry": []}, {"entry": [{"resource": {"resourceType": "Practitioner"}}]}):
        assert FhirPlanNetSource._in_network(bundle) is not True


# ── Rule 2: a cached "unknown" maps to None — never True, never a "no" ─────────
def test_unknown_cache_value_maps_to_none():
    assert _FHIR_STR_TO_VALUE["unknown"] is None
    assert _FHIR_STR_TO_VALUE["in_network"] is True
    assert _FHIR_STR_TO_VALUE["not_found"] is False


# ── Rule 3: the estimated tier can never assert verified, and never False ──────
@pytest.mark.parametrize("states", [None, ["CA"], ["CA", "NY"]])
@pytest.mark.parametrize("ctx_state", ["", "CA", "TX", "ny"])
@pytest.mark.asyncio
async def test_estimate_only_emits_true_or_unknown_and_stays_estimated(states, ctx_state):
    src = EstimatedPayerSource({"id": "p", "label": "P", "category": "commercial", "states": states})
    assert src.confidence == "estimated"
    out = await src.check_many_ctx({"1": {"state": ctx_state}})
    assert out["1"] in (True, None)  # NEVER False — absence isn't evidence of rejection
    # An estimate carries no provenance, so it can never look traceable/verified.
    assert src.provenance_many(["1"]) == {}


# ── Rule 4: every verified True answer carries provenance (source_url + date) ──
@respx.mock
@pytest.mark.asyncio
async def test_every_verified_true_result_carries_provenance(temp_db, monkeypatch):
    """A green badge must be traceable. Build a registry whose verified sources span
    Medicare, TiC, and FHIR, then assert NO verified True result lacks a non-empty
    source_url and fetched_at."""
    db.medicare_add_many(["1003000126"])
    db.source_meta_set("medicare", "https://data.cms.gov/enrollment", 1700000000.0)
    db.tic_add_many("aetna", ["1003000126"])
    db.source_meta_set("aetna", "https://aetna.example/innetwork.json", 1700000001.0)
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [{
        "id": "cigna", "label": "Cigna", "base_url": base,
        "verify_url": "https://cigna.example/find"}])
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))

    reg = _build()
    ann = await reg.annotate([{"npi": "1003000126", "stateAb": "CA"}])
    verified_true = [(pid, r) for pid, r in ann["1003000126"].items()
                     if r["confidence"] == "verified" and r["value"] is True]
    assert verified_true, "expected at least one verified True result to check"
    for pid, r in verified_true:
        assert r.get("source_url"), f"{pid} verified True without a source_url"
        assert r.get("fetched_at"), f"{pid} verified True without a fetched_at"


# ── Rule 5: unknown is never promoted to a result value of True by the merge ───
@pytest.mark.asyncio
async def test_unknown_sources_produce_no_true(temp_db):
    """When every source for a plan returns None (unknown), the merge must not invent
    a True. Kaiser is regional (CA, CO, …) and has no verified data here, so for a TX
    provider it returns None — the result must be omitted, never a fabricated yes."""
    reg = _build()
    ann = await reg.annotate([{"npi": "9999999999", "stateAb": "TX"}], only=["kaiser"])
    info = ann["9999999999"].get("kaiser")
    assert info is None or info["value"] is not True


# ── Rule 6: confidence + provenance are mutually consistent across the catalog ─
@pytest.mark.asyncio
async def test_estimated_results_never_verified_or_provenanced(temp_db):
    reg = _build()
    ann = await reg.annotate([{"npi": "1", "stateAb": "CA"}])
    for _plan_id, r in ann["1"].items():
        if r["confidence"] == "estimated":
            assert r["value"] in (True, None)
            assert "source_url" not in r and "fetched_at" not in r


def test_confidence_rank_keeps_verified_above_estimated():
    """The merge ranking that lets a verified answer supersede an estimate must hold —
    if this inverts, an estimate could mask a real verified/False answer."""
    assert insurance._CONFIDENCE_RANK["verified"] > insurance._CONFIDENCE_RANK["estimated"]
