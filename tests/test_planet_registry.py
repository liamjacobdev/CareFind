"""C1: the public FHIR Plan-Net registry, its rigorous validator, and auto-wiring.

The validator is the trust gate for verified breadth: it must mark an endpoint
`validated` only when it answers the per-NPI lookup truthfully, and refuse one that
fabricates a "yes" (ignores the NPI filter) or a "no" (returns nothing for listed NPIs).
"""
import httpx
import pytest
import respx

from app import planet_registry, verify_payers
from app.config import settings
from app.insurance import FhirPlanNetSource, Registry
from app.planet_registry import PlanNetEndpoint

_SYS = "http://hl7.org/fhir/sid/us-npi"
_NET_ROLE = {"resourceType": "PractitionerRole", "active": True,
             "network": [{"reference": "Network/x"}]}


def _ep(base: str, **kw) -> PlanNetEndpoint:
    return PlanNetEndpoint(id="demo", label="Demo", base_url=base, category="medicaid",
                           states=["MD"], **kw)


def _mock_directory(base, *, real_npi="1111111111", bogus_returns_role=False,
                    real_returns_role=True):
    """Mock a Plan-Net directory's /Practitioner + /PractitionerRole with configurable
    (mis)behavior, so we can prove the validator's verdicts."""
    respx.get(f"{base}/Practitioner").mock(return_value=httpx.Response(200, json={
        "resourceType": "Bundle", "entry": [
            {"resource": {"resourceType": "Practitioner",
                          "identifier": [{"system": _SYS, "value": real_npi}]}}]}))

    def pr_handler(request):
        ident = request.url.params.get("practitioner.identifier", "")
        if ident.endswith(f"|{real_npi}"):
            roles = [{"resource": _NET_ROLE}] if real_returns_role else []
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": roles})
        if "|" in ident:  # a filtered query for some other (bogus) NPI
            roles = [{"resource": _NET_ROLE}] if bogus_returns_role else []
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": roles})
        return httpx.Response(200, json={"resourceType": "Bundle", "total": 42, "entry": []})

    respx.get(f"{base}/PractitionerRole").mock(side_effect=pr_handler)


@respx.mock
def test_validator_marks_a_truthful_endpoint_validated():
    base = "https://good.example/r4"
    _mock_directory(base)
    with httpx.Client() as c:
        res = verify_payers.probe(c, _ep(base))
    assert res["status"] == "validated"
    assert res["total"] == 42


@respx.mock
def test_validator_rejects_endpoint_that_ignores_the_npi_filter():
    """A directory that returns providers for a BOGUS NPI would fabricate in-network for
    everyone — it must be 'unusable', never validated."""
    base = "https://ignores-filter.example/r4"
    _mock_directory(base, bogus_returns_role=True)
    with httpx.Client() as c:
        res = verify_payers.probe(c, _ep(base))
    assert res["status"] == "unusable"
    assert "ignores the NPI filter" in res["error"]


@respx.mock
def test_validator_rejects_endpoint_that_returns_nothing_for_listed_npis():
    """A directory that returns no role for a real listed NPI would fabricate a 'no' — it
    must be 'unusable', never validated."""
    base = "https://always-empty.example/r4"
    _mock_directory(base, real_returns_role=False)
    with httpx.Client() as c:
        res = verify_payers.probe(c, _ep(base))
    assert res["status"] == "unusable"


@respx.mock
def test_validator_validates_two_step_endpoint_requiring_a_search_param():
    """The Excellus class: a compliant directory that rejects an unfiltered browse (requires
    a search param) and offers no chained search, yet answers the two_step per-NPI lookup
    truthfully. The validator must discover a listed NPI via a filtered (family) browse,
    tolerate the non-Bundle head-check, and still validate it — without weakening either
    trust gate."""
    base = "https://needs-param.example/r4"
    real_npi, real_pid = "1003815440", "P-123"
    _prac = {"resourceType": "Practitioner", "id": real_pid,
             "identifier": [{"system": _SYS, "value": real_npi}]}
    _reject = httpx.Response(400, json={"resourceType": "OperationOutcome",
                                        "issue": [{"severity": "error", "code": "processing"}]})

    def practitioner_handler(request):
        params = request.url.params
        ident = params.get("identifier")
        if ident is not None:  # two_step first leg — filters correctly by NPI
            listed = ident.endswith(f"|{real_npi}")
            return httpx.Response(200, json={"resourceType": "Bundle",
                                             "total": 1 if listed else 0,
                                             "entry": [{"resource": _prac}] if listed else []})
        if params.get("family"):  # filtered discovery browse works
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": _prac}]})
        return _reject  # unfiltered browse rejected (requires a search param)

    def role_handler(request):
        if request.url.params.get("practitioner") == f"Practitioner/{real_pid}":
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": _NET_ROLE}]})
        return _reject  # unfiltered head-check browse is not a Bundle — must not disqualify

    respx.get(f"{base}/Practitioner").mock(side_effect=practitioner_handler)
    respx.get(f"{base}/PractitionerRole").mock(side_effect=role_handler)
    with httpx.Client() as c:
        res = verify_payers.probe(c, _ep(base, lookup_mode="two_step"))
    assert res["status"] == "validated", res


@respx.mock
def test_validator_flags_gated_endpoint():
    base = "https://gated.example/r4"
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(401))
    with httpx.Client() as c:
        res = verify_payers.probe(c, _ep(base))
    assert res["status"] == "gated"


def test_ledger_renders_status_table():
    results = verify_payers.validate(offline=True)
    md = verify_payers.render_ledger(results)
    assert "Provenance ledger" in md and "| Payer / program |" in md
    # Every registry entry appears with its catalog id.
    for e in planet_registry.REGISTRY:
        assert f"`{e.id}`" in md


def test_validated_payer_configs_gated_by_flag(monkeypatch):
    monkeypatch.setattr(settings, "use_planet_registry", False)
    assert planet_registry.validated_payer_configs() == []
    monkeypatch.setattr(settings, "use_planet_registry", True)
    cfgs = planet_registry.validated_payer_configs()
    ids = {c["id"] for c in cfgs}
    # Only validated endpoints are emitted; unusable/gated ones never wire.
    assert ids == {e.id for e in planet_registry.validated()}
    assert "premera_bcbs" not in ids and "ct_medicaid" not in ids


@pytest.mark.asyncio
async def test_validated_endpoint_graduates_to_verified_filter(temp_db, monkeypatch):
    """A validated registry endpoint is wired as a verified ('Confirmed') filter out of
    the box (no payers.json), and supersedes its estimated catalog entry."""
    monkeypatch.setattr(settings, "use_planet_registry", True)
    base = "https://pp.example/r4"
    monkeypatch.setattr(planet_registry, "validated_payer_configs", lambda: [
        {"id": "priority_partners", "label": "PP", "payer": "priority_partners",
         "category": "medicaid", "base_url": base, "states": ["MD"],
         "npi_system": _SYS, "verify_url": base}])
    with respx.mock:
        respx.get(f"{base}/PractitionerRole").mock(
            return_value=httpx.Response(200, json={"entry": [{"resource": _NET_ROLE}]}))
        reg = Registry()
        reg.build()
        pp = next(p for p in reg.plans() if p["id"] == "priority_partners")
        assert pp["confidence"] == "verified"  # graduated from estimated
        ann = await reg.annotate([{"npi": "1003000126", "stateAb": "MD"}], only=["priority_partners"])
    assert ann["1003000126"]["priority_partners"]["confidence"] == "verified"
    assert ann["1003000126"]["priority_partners"]["value"] is True


@respx.mock
@pytest.mark.asyncio
async def test_regional_fhir_source_only_queries_in_state_npis(temp_db):
    """A regional verified source must not hammer its directory with — or fabricate
    answers for — out-of-state providers: out-of-state NPIs return None with no call."""
    base = "https://md-only.example/r4"
    route = respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json={"entry": [{"resource": _NET_ROLE}]}))
    src = FhirPlanNetSource({"id": "advantage_md", "label": "Adv MD", "base_url": base,
                             "category": "medicare_advantage", "states": ["MD"]})
    out = await src.check_many_ctx({"1111111111": {"state": "MD"}, "2222222222": {"state": "CA"}})
    assert out["1111111111"] is True       # in-state, confirmed
    assert out["2222222222"] is None       # out-of-state -> unknown, never fabricated
    assert route.call_count == 1           # the CA provider was never queried live
