"""The two-tier insurance model: confidence, regional gating, FHIR, merging."""
import httpx
import pytest
import respx

from app import db, sources
from app.config import settings
from app.insurance import EstimatedPayerSource, FhirPlanNetSource, Registry

_IN_NETWORK = {"entry": [{"resource": {"resourceType": "PractitionerRole",
                                       "active": True, "network": [{"reference": "Organization/x"}]}}]}


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
    assert ann["1003000126"]["medicare"] == {
        "value": True, "confidence": "verified", "source": "medicare", "level": "plan"}
    assert ann["9999999999"]["medicare"]["value"] is False


@pytest.mark.asyncio
async def test_regional_payer_state_scoped_and_graduates_when_wired(temp_db, monkeypatch):
    """The live-validated regional payers are state-scoped estimates by default
    (Premera only WA/AK), and graduate to verified when their FHIR endpoint is wired
    — proving the catalog id is the stable join key (no UI change needed)."""
    # Estimated, correctly scoped: WA gets a "likely", a non-served state gets nothing.
    reg = _build()
    ann = await reg.annotate(
        [{"npi": "1", "stateAb": "WA"}, {"npi": "2", "stateAb": "FL"}], only=["premera_bcbs"])
    assert ann["1"]["premera_bcbs"]["confidence"] == "estimated"
    assert "premera_bcbs" not in ann["2"]  # not offered outside WA/AK

    # Wire its FHIR endpoint -> same id now answers verified, superseding the estimate.
    base = "https://opala.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "premera_bcbs", "label": "Premera", "base_url": base, "category": "commercial"}])
    with respx.mock:
        respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))
        reg2 = Registry()
        reg2.build()
        ann2 = await reg2.annotate([{"npi": "1", "stateAb": "WA"}], only=["premera_bcbs"])
    r = ann2["1"]["premera_bcbs"]
    assert (r["value"], r["confidence"], r["source"], r["level"]) == (
        True, "verified", "premera_bcbs", "payer")
    assert r["source_url"] == base and r["fetched_at"] > 0  # provenance attached


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
    monkeypatch.setattr(sources, "_AVAILABILITY_TTL", 0.0)
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


# ── A2: payer-level network listing vs plan-level acceptance ───────────────────
def test_plans_carry_level_payer_vs_plan(temp_db):
    """Every emitted plan declares its level. Medicare is plan-level (a single
    program); commercial network directories are payer-level."""
    db.medicare_add_many(["1003000126"])
    reg = _build()
    by_id = {p["id"]: p for p in reg.plans()}
    assert by_id["medicare"]["level"] == "plan"
    # Catalog commercial payers are payer-level (a network directory, not a plan).
    assert by_id["aetna"]["level"] == "payer"
    assert by_id["cigna"]["level"] == "payer"
    # Every plan declares one of the two valid levels — no silent omissions.
    assert all(p["level"] in ("payer", "plan") for p in reg.plans())


@pytest.mark.asyncio
async def test_annotate_results_carry_level(temp_db):
    """A per-provider answer must say whether it's a payer-directory listing or a
    plan-level confirmation, so the UI can avoid implying plan acceptance."""
    db.medicare_add_many(["1003000126"])
    db.tic_add_many("aetna", ["1003000126"])
    reg = _build()
    ann = await reg.annotate(
        [{"npi": "1003000126", "stateAb": "CA"}], only=["medicare", "aetna"])
    assert ann["1003000126"]["medicare"]["level"] == "plan"
    # A TiC ingest is a payer network listing, not a specific-plan confirmation.
    assert ann["1003000126"]["aetna"]["level"] == "payer"


# ── A4: national "operates here" estimates are non-filtering ───────────────────
def test_plans_mark_national_estimates_non_filterable(temp_db):
    """A national estimate can't narrow results, so it's flagged non-filterable;
    a regional estimate and any verified plan stay filterable."""
    db.medicare_add_many(["1003000126"])
    reg = _build()
    by_id = {p["id"]: p for p in reg.plans()}
    assert by_id["aetna"]["filterable"] is False        # national estimate
    assert by_id["kaiser"]["filterable"] is True         # regional estimate (CA, CO, …)
    assert by_id["premera_bcbs"]["filterable"] is True   # regional estimate (WA/AK)
    assert by_id["medicare"]["filterable"] is True       # verified


def test_verified_estimate_for_same_payer_is_filterable(temp_db):
    """If a national payer gains a verified source, the plan becomes filterable again
    (the verified source discriminates)."""
    db.tic_add_many("aetna", ["1003000126"])
    reg = _build()
    by_id = {p["id"]: p for p in reg.plans()}
    assert by_id["aetna"]["filterable"] is True


# ── A3: every verified answer carries source URL + fetch date ──────────────────
@pytest.mark.asyncio
async def test_verified_medicare_carries_provenance(temp_db):
    db.medicare_add_many(["1003000126"])
    db.source_meta_set("medicare", "https://data.cms.gov/enrollment", 1700000000.0)
    reg = _build()
    ann = await reg.annotate([{"npi": "1003000126", "stateAb": "FL"}], only=["medicare"])
    r = ann["1003000126"]["medicare"]
    assert r["value"] is True and r["confidence"] == "verified"
    assert r["source_url"] == "https://data.cms.gov/enrollment"
    assert r["fetched_at"] == 1700000000.0


@respx.mock
@pytest.mark.asyncio
async def test_verified_fhir_carries_per_npi_provenance(temp_db, monkeypatch):
    """A FHIR verified hit carries the payer's verify URL and the cache fetch date."""
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [{
        "id": "cigna", "label": "Cigna", "base_url": base,
        "verify_url": "https://cigna.example/find-a-doctor"}])
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))
    reg = Registry()
    reg.build()
    ann = await reg.annotate([{"npi": "1111111111", "stateAb": "CA"}], only=["cigna"])
    r = ann["1111111111"]["cigna"]
    assert r["value"] is True
    assert r["source_url"] == "https://cigna.example/find-a-doctor"
    assert isinstance(r["fetched_at"], float) and r["fetched_at"] > 0


@respx.mock
@pytest.mark.asyncio
async def test_oauth_source_attaches_bearer_token(temp_db, monkeypatch):
    """An OAuth-gated payer: the live source fetches a client-credentials token and sends
    it as a Bearer on the FHIR call. Without the Bearer the endpoint 401s -> 'unknown',
    never a fabricated yes."""
    base = "https://gated.example/r4"
    monkeypatch.setenv("AETNA_ID", "cid")
    monkeypatch.setenv("AETNA_SECRET", "sec")
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "aetna", "label": "Aetna", "base_url": base, "auth": "oauth2",
         "token_url": "https://idp.example/token",
         "client_id_env": "AETNA_ID", "client_secret_env": "AETNA_SECRET"}])
    token = respx.post("https://idp.example/token").mock(
        return_value=httpx.Response(200, json={"access_token": "BTOK", "expires_in": 3600}))

    def pr(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bearer BTOK":
            return httpx.Response(401, json={"resourceType": "OperationOutcome"})
        return httpx.Response(200, json=_IN_NETWORK)

    roles = respx.get(f"{base}/PractitionerRole").mock(side_effect=pr)
    reg = Registry()
    reg.build()
    res = await reg.annotate([{"npi": "1111111111", "stateAb": "PA"}], only=["aetna"])
    assert res["1111111111"]["aetna"]["value"] is True
    assert res["1111111111"]["aetna"]["confidence"] == "verified"
    assert token.called and roles.called


@respx.mock
@pytest.mark.asyncio
async def test_two_step_lookup_resolves_practitioner_then_roles(temp_db, monkeypatch):
    """For a directory that doesn't support the chained practitioner.identifier search
    (lookup_mode='two_step', e.g. UnitedHealthcare/Optum): resolve the Practitioner by
    NPI, then its roles. A listed NPI -> in-network; a bogus NPI (no Practitioner) ->
    False, never a fabricated yes."""
    base = "https://optum.example/R4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "unitedhealthcare", "label": "UHC", "base_url": base, "lookup_mode": "two_step"}])

    def practitioner_resp(request: httpx.Request) -> httpx.Response:
        ident = request.url.params.get("identifier", "")
        if ident.endswith("|1111111111"):
            return httpx.Response(200, json={"entry": [
                {"resource": {"resourceType": "Practitioner", "id": "P1"}}]})
        return httpx.Response(200, json={"entry": []})  # bogus -> no Practitioner

    prac = respx.get(f"{base}/Practitioner").mock(side_effect=practitioner_resp)
    roles = respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_IN_NETWORK))
    reg = Registry()
    reg.build()

    real = await reg.annotate([{"npi": "1111111111", "stateAb": "NY"}], only=["unitedhealthcare"])
    assert real["1111111111"]["unitedhealthcare"]["value"] is True
    assert real["1111111111"]["unitedhealthcare"]["confidence"] == "verified"

    bogus = await reg.annotate([{"npi": "0000000000", "stateAb": "NY"}], only=["unitedhealthcare"])
    assert bogus["0000000000"]["unitedhealthcare"]["value"] is False  # no Practitioner -> not in-network
    assert prac.called and roles.called


@respx.mock
@pytest.mark.asyncio
async def test_network_source_queried_only_on_demand(temp_db, monkeypatch):
    """A live FHIR Plan-Net source costs a per-NPI directory call, so it must NOT fire on
    an unfiltered search (only=None) — only when its plan is explicitly requested.
    Otherwise every default search would hit the payer's directory for the whole pool."""
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "cigna", "label": "Cigna", "base_url": base}])
    route = respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_IN_NETWORK))
    reg = Registry()
    reg.build()

    # Unfiltered search: the live directory is never called.
    await reg.annotate([{"npi": "1111111111", "stateAb": "CA"}], only=None)
    assert route.call_count == 0

    # Requested explicitly: now it fires and returns a verified answer.
    ann = await reg.annotate([{"npi": "1111111111", "stateAb": "CA"}], only=["cigna"])
    assert route.call_count >= 1
    assert ann["1111111111"]["cigna"]["confidence"] == "verified"


@pytest.mark.asyncio
async def test_estimated_results_carry_no_provenance(temp_db):
    """An estimate must never acquire a source link/date — that's reserved for
    verified answers, so an estimate can't masquerade as traceable."""
    reg = _build()
    ann = await reg.annotate([{"npi": "9", "stateAb": "CA"}], only=["aetna"])
    r = ann["9"]["aetna"]
    assert r["confidence"] == "estimated"
    assert "source_url" not in r and "fetched_at" not in r


# ── A1: presence-only must never become a Confirmed yes ───────────────────────
def _role(active=True, network=True, healthcare=False):
    res = {"resourceType": "PractitionerRole"}
    if active is not None:
        res["active"] = active
    if network:
        res["network"] = [{"reference": "Network/abc"}]
    if healthcare:
        res["healthcareService"] = [{"reference": "HealthcareService/x"}]
    return {"entry": [{"resource": res}]}


def test_in_network_active_with_network_is_true():
    assert FhirPlanNetSource._in_network(_role(active=True, network=True)) is True
    # A Plan-Net network-reference extension also counts as a resolvable link.
    ext_bundle = {"entry": [{"resource": {
        "resourceType": "PractitionerRole", "active": True,
        "extension": [{"url": "http://hl7.org/fhir/us/davinci-pdex-plan-net/StructureDefinition/network-reference",
                       "valueReference": {"reference": "Network/abc"}}]}}]}
    assert FhirPlanNetSource._in_network(ext_bundle) is True


def test_in_network_presence_only_is_unknown_not_true():
    """Listed (active role) but no resolvable network link → None, never True."""
    assert FhirPlanNetSource._in_network(_role(active=True, network=False)) is None
    # healthcareService is NOT a network link — presence of it alone is still unknown.
    assert FhirPlanNetSource._in_network(
        _role(active=True, network=False, healthcare=True)) is None


def test_in_network_inactive_only_is_never_true():
    """An only-inactive listing is listed-but-unconfirmable (None), never True/False yes."""
    assert FhirPlanNetSource._in_network(_role(active=False, network=True)) is None
    assert FhirPlanNetSource._in_network(_role(active=False, network=False)) is None


def test_in_network_not_listed_is_false():
    assert FhirPlanNetSource._in_network({"entry": []}) is False
    assert FhirPlanNetSource._in_network({}) is False


def test_in_network_strictness_flag_toggles_presence_only():
    """The strictness flag flips presence-only between unknown (network) and yes
    (directory). The default is the trust-preserving 'network'."""
    presence_only = _role(active=True, network=False)
    assert FhirPlanNetSource._in_network(presence_only, strictness="network") is None
    assert FhirPlanNetSource._in_network(presence_only, strictness="directory") is True
    # 'directory' still never turns a not-listed or inactive-only bundle into True.
    assert FhirPlanNetSource._in_network({"entry": []}, strictness="directory") is False
    assert FhirPlanNetSource._in_network(
        _role(active=False, network=False), strictness="directory") is None


@respx.mock
@pytest.mark.asyncio
async def test_fhir_check_many_concurrent_mapping(temp_db):
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


@respx.mock
@pytest.mark.asyncio
async def test_fhir_upstream_error_degrades_and_logs(temp_db, caplog):
    """A failing FHIR source must not crash the request: it degrades to 'unknown'
    (None) AND emits a WARNING naming the payer/NPI (T1.2 observability)."""
    base = "https://payer.example/r4"
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(503))
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})

    with caplog.at_level("WARNING", logger="innetwork.insurance"):
        out = await src.check_many(["1111111111"])

    assert out["1111111111"] is None  # degraded, not fabricated
    assert any("FHIR Plan-Net check failed" in r.message and "demo" in r.message
               for r in caplog.records)


# ── T3.1: FHIR Plan-Net result cache ──────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_fhir_cache_warm_hit_makes_no_live_calls(temp_db):
    base = "https://payer.example/r4"
    route = respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})

    cold = await src.check_many(["1111111111", "2222222222"])
    assert route.call_count == 2  # one live call per NPI on the cold miss
    assert cold == {"1111111111": True, "2222222222": True}

    warm = await src.check_many(["1111111111", "2222222222"])
    assert route.call_count == 2  # zero additional live calls — served from cache
    assert warm == cold
    # The cold result was persisted with a definite value.
    assert db.fhir_cache_get("demo", "1111111111")[0] == "in_network"


@respx.mock
@pytest.mark.asyncio
async def test_fhir_cache_partial_hit_fetches_only_misses(temp_db):
    base = "https://payer.example/r4"
    route = respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})

    await src.check_many(["1111111111"])          # warms one NPI
    assert route.call_count == 1
    await src.check_many(["1111111111", "3333333333"])  # only the new NPI is live
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_fhir_cache_unknown_never_served_as_no(temp_db, monkeypatch):
    """An endpoint error is cached as 'unknown' and must map back to None — never to
    False (not-in-network). A fresh 'unknown' is served from cache without re-hitting."""
    monkeypatch.setattr(settings, "fhir_cache_unknown_ttl", 600)
    base = "https://payer.example/r4"
    route = respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(503))
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})

    assert (await src.check_many(["1111111111"]))["1111111111"] is None
    assert db.fhir_cache_get("demo", "1111111111")[0] == "unknown"
    # Warm read within the unknown TTL: still None (not False), no extra live call.
    assert (await src.check_many(["1111111111"]))["1111111111"] is None
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_fhir_cache_stale_unknown_refetches_and_recovers(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "fhir_cache_unknown_ttl", 0)  # 'unknown' always stale
    base = "https://payer.example/r4"
    route = respx.get(f"{base}/PractitionerRole")
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})

    route.mock(return_value=httpx.Response(503))
    assert (await src.check_many(["1111111111"]))["1111111111"] is None  # unknown
    route.mock(return_value=httpx.Response(200, json=_IN_NETWORK))       # endpoint recovers
    assert (await src.check_many(["1111111111"]))["1111111111"] is True  # re-fetched, not stuck


@respx.mock
@pytest.mark.asyncio
async def test_fhir_wired_payer_returns_verified(temp_db, monkeypatch):
    """T3.2: a payer wired via FHIR Plan-Net (payers.json) returns confidence
    'verified' for a known in-network NPI, and that verified answer supersedes the
    payer's estimated catalog entry. (respx-mocked stand-in for a live Plan-Net
    endpoint; see the README for the validated real endpoints + manual live-check.)"""
    base = "https://api.payer.example/r4"
    # Wire 'cigna' (a catalog id) to a FHIR Plan-Net endpoint, as payers.json would.
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "cigna", "label": "Cigna", "base_url": base, "category": "commercial"}])
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))

    reg = Registry()
    reg.build()
    # The plan now reports verified confidence (FHIR supersedes the estimate).
    cigna_plan = next(p for p in reg.plans() if p["id"] == "cigna")
    assert cigna_plan["confidence"] == "verified"

    ann = await reg.annotate([{"npi": "1003000126", "stateAb": "CA"}], only=["cigna"])
    r = ann["1003000126"]["cigna"]
    assert (r["value"], r["confidence"], r["source"], r["level"]) == (
        True, "verified", "cigna", "payer")
    assert r["source_url"] == base and r["fetched_at"] > 0


@respx.mock
@pytest.mark.asyncio
async def test_fhir_cache_stale_definite_refetches(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "fhir_cache_ttl", 0)  # definite answers always stale
    base = "https://payer.example/r4"
    route = respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_IN_NETWORK))
    src = FhirPlanNetSource({"id": "demo", "label": "Demo", "base_url": base})

    await src.check_many(["1111111111"])
    await src.check_many(["1111111111"])
    assert route.call_count == 2  # stale definite entry was re-fetched, not served
