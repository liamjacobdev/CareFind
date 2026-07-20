"""The FHIR bulk harvester (Rail 1). Same trust bar as the live path: an NPI is admitted
only for an active PractitionerRole with a resolvable network link (presence/inactive/
no-link never counts), every NPI passes the Luhn gate, and pagination + resumption work
without ever shipping a silently-short "verified" set."""
import httpx
import pytest
import respx

from app import harvest_fhir, membership
from app.config import settings
from app.harvest_fhir import HarvestStats, harvest_endpoint, harvest_page
from app.insurance import MembershipSource

# Valid NPIs (pass Luhn). NPI_A/B are in-network; NPI_C is a Luhn-INVALID impostor.
NPI_A, NPI_B = "1003000126", "1003000134"
NPI_BAD = "1234567890"


def _practitioner(pid, npi):
    return {"fullUrl": f"http://x/Practitioner/{pid}",
            "resource": {"resourceType": "Practitioner", "id": pid,
                         "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": npi}]}}


def _role(pid, *, active=True, network=True):
    res = {"resourceType": "PractitionerRole", "active": active,
           "practitioner": {"reference": f"Practitioner/{pid}"}}
    if network:
        res["extension"] = [{
            "url": "http://hl7.org/fhir/us/davinci-pdex-plan-net/StructureDefinition/network-reference",
            "valueReference": {"reference": "Organization/net1"}}]
    return {"resource": res}


def _bundle(entries, next_url=None):
    b = {"resourceType": "Bundle", "entry": entries}
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


# ── the per-role trust judgement, applied in bulk ─────────────────────────────
def test_harvest_page_admits_only_active_network_linked():
    entries = [
        _practitioner("a", NPI_A), _role("a"),                          # admit
        _practitioner("b", NPI_B), _role("b", active=False),            # inactive -> skip
        _practitioner("c", NPI_A), _role("c", network=False),           # no network link -> skip
    ]
    out, stats = set(), HarvestStats()
    harvest_page(_bundle(entries), out, stats)
    assert out == {NPI_A}
    assert stats.roles_seen == 3 and stats.roles_in_network == 1


def test_harvest_page_luhn_gate_rejects_impostor_npi():
    entries = [_practitioner("a", NPI_BAD), _role("a")]  # active + network but bad NPI
    out, stats = set(), HarvestStats()
    harvest_page(_bundle(entries), out, stats)
    assert out == set()
    # It counted as in-network but the practitioner index dropped the bad NPI, so the role
    # resolves to no practitioner (unresolved) rather than admitting a fabricated NPI.
    assert stats.roles_in_network == 1 and stats.npis_admitted == 0


def test_harvest_page_counts_unresolved_practitioner():
    entries = [_role("missing")]  # in-network role whose Practitioner isn't on the page
    out, stats = set(), HarvestStats()
    harvest_page(_bundle(entries), out, stats)
    assert out == set() and stats.practitioner_unresolved == 1


# ── pagination, bounds, resumption ────────────────────────────────────────────
@respx.mock
def test_harvest_follows_next_link_across_pages():
    base = "https://payer.example/r4"
    page2 = f"{base}/PractitionerRole?page=2"
    respx.get(f"{base}/PractitionerRole", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("b", NPI_B), _role("b")])))
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")],
                                                      next_url=page2)))
    out, stats = harvest_endpoint({"id": "p", "base_url": base}, page_size=2)
    assert out == {NPI_A, NPI_B}
    assert stats.pages == 2 and stats.next_cursor is None


@respx.mock
def test_max_pages_bounds_and_records_cursor():
    base = "https://payer.example/r4"
    page2 = f"{base}/PractitionerRole?page=2"
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")],
                                                      next_url=page2)))
    out, stats = harvest_endpoint({"id": "p", "base_url": base}, page_size=2, max_pages=1)
    assert out == {NPI_A}
    assert stats.pages == 1 and stats.next_cursor == page2   # resumable


@respx.mock
def test_operationoutcome_page_stops_loudly():
    """A FHIR server wraps errors (too-large _count, rejected facet) in a 200 searchset
    Bundle. That must stop the harvest with an error + cursor, not look like an empty
    directory and ship an empty verified set."""
    base = "https://payer.example/r4"
    oo = {"resourceType": "Bundle", "entry": [{"resource": {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": "Error code CRD16-005"}]}}]}
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=oo))
    out, stats = harvest_endpoint({"id": "p", "base_url": base})
    assert out == set() and stats.error and "CRD16-005" in stats.error


@respx.mock
def test_non_bundle_browse_stops_without_crashing():
    base = "https://payer.example/r4"
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json={"resourceType": "OperationOutcome"}))
    out, stats = harvest_endpoint({"id": "p", "base_url": base})
    assert out == set() and stats.error and "Bundle" in stats.error


# ── end-to-end: harvest -> bitmap -> MembershipSource serves it verified ───────
@respx.mock
@pytest.mark.asyncio
async def test_harvest_to_bitmap_is_served_verified(tmp_path, monkeypatch):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "cigna", "label": "Cigna", "category": "commercial", "base_url": base,
         "verify_url": "https://cigna.example/find"}])
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle(
            [_practitioner("a", NPI_A), _role("a"), _practitioner("b", NPI_B), _role("b")])))

    entry, stats = harvest_fhir.harvest_to_bitmap("cigna", tmp_path)
    assert entry is not None and entry.method == "fhir-plannet" and entry.level == "payer"
    assert entry.count == 2 and entry.source_url == "https://cigna.example/find"

    store = membership.MembershipStore(tmp_path)
    store.load()
    src = MembershipSource(store.entry("cigna"), store)
    assert src.confidence == "verified" and src.requires_network is False
    out = await src.check_many([NPI_A, NPI_B, "1992999874"])
    assert out[NPI_A] is True and out[NPI_B] is True
    assert out["1992999874"] is False        # not harvested -> genuine no
    store.close()


@respx.mock
def test_empty_harvest_writes_nothing(tmp_path, monkeypatch):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "cigna", "label": "Cigna", "base_url": base}])
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_bundle([])))
    entry, stats = harvest_fhir.harvest_to_bitmap("cigna", tmp_path)
    assert entry is None                      # a failed/empty harvest never ships an empty set
    assert not (tmp_path / "cigna.roaring").exists()


@respx.mock
def test_partial_harvest_is_not_written_as_complete(tmp_path, monkeypatch):
    """The trust guard: a harvest that stopped early (max_pages/timeout/error → a resume
    cursor is left) collected a PARTIAL set. Serving it as complete would make in-network
    providers beyond the harvested pages read as False. So it must NOT be written."""
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "cigna", "label": "Cigna", "base_url": base}])
    page2 = f"{base}/PractitionerRole?page=2"
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")],
                                                      next_url=page2)))
    # Bounded to 1 page while more remain -> next_cursor set -> incomplete.
    entry, stats = harvest_fhir.harvest_to_bitmap("cigna", tmp_path, max_pages=1)
    assert stats.npis_admitted == 1 and stats.next_cursor == page2
    assert entry is None and not (tmp_path / "cigna.roaring").exists()
    # A caller that explicitly opts out of the completeness guard may still write.
    entry2, _ = harvest_fhir.harvest_to_bitmap("cigna", tmp_path, max_pages=1, complete_only=False)
    assert entry2 is not None and (tmp_path / "cigna.roaring").exists()


# ── helpers + CLI coverage ────────────────────────────────────────────────────
def test_headers_includes_api_key_and_no_oauth():
    with httpx.Client() as c:
        h = harvest_fhir._headers({"api_key_header": "X-Key", "api_key": "secret"}, c)
    assert h["X-Key"] == "secret" and "Authorization" not in h


def test_resolve_cfg_prefers_payers_json_then_registry(monkeypatch):
    monkeypatch.setattr(settings, "load_payers", lambda: [{"id": "cigna", "base_url": "u"}])
    assert harvest_fhir._resolve_cfg("cigna")["base_url"] == "u"
    monkeypatch.setattr(settings, "load_payers", lambda: [])
    assert harvest_fhir._resolve_cfg("cigna")["id"] == "cigna"   # from planet_registry
    with pytest.raises(SystemExit):
        harvest_fhir._resolve_cfg("nope_payer")


@respx.mock
def test_get_with_backoff_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(harvest_fhir.time, "sleep", lambda _s: None)  # no real backoff wait
    base = "https://payer.example/r4"
    respx.get(f"{base}/PractitionerRole").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")])),
    ])
    out, stats = harvest_endpoint({"id": "p", "base_url": base})
    assert out == {NPI_A} and stats.retries >= 1


@respx.mock
def test_get_with_backoff_gives_up_and_records_cursor(monkeypatch):
    monkeypatch.setattr(harvest_fhir.time, "sleep", lambda _s: None)
    base = "https://payer.example/r4"
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(503))
    out, stats = harvest_endpoint({"id": "p", "base_url": base})
    assert out == set() and stats.error and stats.next_cursor is not None


@respx.mock
def test_cli_dry_run_reports_without_writing(tmp_path, monkeypatch, capsys):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [{"id": "cigna", "label": "Cigna", "base_url": base}])
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path))
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")])))
    harvest_fhir.main(["harvest_fhir", "cigna", "--dry-run", "--page-size", "50"])
    assert "cigna" in capsys.readouterr().out
    assert not (tmp_path / "cigna.roaring").exists()


@respx.mock
def test_cli_complete_harvest_writes(tmp_path, monkeypatch, capsys):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [{"id": "cigna", "label": "Cigna", "base_url": base}])
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path))
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")])))
    harvest_fhir.main(["harvest_fhir", "cigna"])
    assert (tmp_path / "cigna.roaring").exists() and "wrote" in capsys.readouterr().out


@respx.mock
def test_cli_shard_facet_and_incomplete_not_written(tmp_path, monkeypatch, capsys):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [{"id": "cigna", "label": "Cigna", "base_url": base}])
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path))
    page2 = f"{base}/PractitionerRole?page=2"
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")], next_url=page2)))
    harvest_fhir.main(["harvest_fhir", "cigna", "--max-pages", "1", "--shard", "state=CA"])
    out = capsys.readouterr().out
    assert "incomplete" in out and not (tmp_path / "cigna.roaring").exists()


@respx.mock
def test_max_npis_bound_records_cursor():
    base = "https://payer.example/r4"
    page2 = f"{base}/PractitionerRole?page=2"
    respx.get(f"{base}/PractitionerRole").mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")], next_url=page2)))
    out, stats = harvest_endpoint({"id": "p", "base_url": base}, max_npis=1)
    assert out == {NPI_A} and stats.next_cursor == page2   # stopped at the NPI budget


@respx.mock
def test_start_url_resumes_from_cursor():
    base = "https://payer.example/r4"
    resume = f"{base}/PractitionerRole?page=5"
    respx.get(f"{base}/PractitionerRole", params={"page": "5"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("b", NPI_B), _role("b")])))
    out, stats = harvest_endpoint({"id": "p", "base_url": base}, start_url=resume)
    assert out == {NPI_B} and stats.pages == 1   # resumed from the cursor, no first-page refetch


# ── sharded (accumulating) harvest for the giants — union + completeness guard ─
@respx.mock
def test_sharded_union_writes_when_all_shards_complete(tmp_path, monkeypatch):
    """A giant walked one facet value at a time in ONE process must UNION the shards' NPIs
    and write once — the fix for `write_payer` overwriting per run (CA then NY would else
    leave only NY)."""
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "humana", "label": "Humana", "base_url": base}])
    respx.get(f"{base}/PractitionerRole", params={"state": "CA"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")])))
    respx.get(f"{base}/PractitionerRole", params={"state": "NY"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("b", NPI_B), _role("b")])))
    entry, outcomes = harvest_fhir.harvest_sharded_to_bitmap(
        "humana", tmp_path, facet="state", values=["CA", "NY"])
    assert entry is not None and entry.count == 2       # unioned, not overwritten
    assert (tmp_path / "humana.roaring").exists()
    assert all(o.complete for o in outcomes) and {o.value for o in outcomes} == {"CA", "NY"}


@respx.mock
def test_sharded_incomplete_shard_writes_nothing(tmp_path, monkeypatch):
    """THE critical trust test: if any shard didn't exhaust (a resume cursor is left), the
    union is a hole-y partial. Serving it as complete would make the missing shard's
    in-network providers read as a fabricated "no" — so nothing is written."""
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "humana", "label": "Humana", "base_url": base}])
    respx.get(f"{base}/PractitionerRole", params={"state": "CA"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")])))
    # NY leaves a next cursor after its one page (max_pages=1) -> incomplete shard.
    respx.get(f"{base}/PractitionerRole", params={"state": "NY"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("b", NPI_B), _role("b")],
                                                      next_url=f"{base}/PractitionerRole?page=2")))
    entry, outcomes = harvest_fhir.harvest_sharded_to_bitmap(
        "humana", tmp_path, facet="state", values=["CA", "NY"], max_pages=1)
    assert entry is None and not (tmp_path / "humana.roaring").exists()
    assert any(not o.complete for o in outcomes)
    # An explicit opt-out of the guard may still write the partial union.
    entry2, _ = harvest_fhir.harvest_sharded_to_bitmap(
        "humana", tmp_path, facet="state", values=["CA", "NY"], max_pages=1, complete_only=False)
    assert entry2 is not None and (tmp_path / "humana.roaring").exists()


@respx.mock
def test_sharded_all_empty_writes_nothing(tmp_path, monkeypatch):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [
        {"id": "humana", "label": "Humana", "base_url": base}])
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(200, json=_bundle([])))
    entry, outcomes = harvest_fhir.harvest_sharded_to_bitmap(
        "humana", tmp_path, facet="state", values=["CA", "NY"])
    assert entry is None and not (tmp_path / "humana.roaring").exists()
    assert all(o.complete and o.npis == 0 for o in outcomes)   # complete but empty -> reported


@respx.mock
def test_cli_facet_values_writes_union(tmp_path, monkeypatch, capsys):
    base = "https://payer.example/r4"
    monkeypatch.setattr(settings, "load_payers", lambda: [{"id": "humana", "label": "Humana", "base_url": base}])
    monkeypatch.setattr(settings, "membership_dir", str(tmp_path))
    respx.get(f"{base}/PractitionerRole", params={"state": "CA"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("a", NPI_A), _role("a")])))
    respx.get(f"{base}/PractitionerRole", params={"state": "NY"}).mock(
        return_value=httpx.Response(200, json=_bundle([_practitioner("b", NPI_B), _role("b")])))
    harvest_fhir.main(["harvest_fhir", "humana", "--facet", "state", "--values", "CA,NY"])
    assert (tmp_path / "humana.roaring").exists() and "wrote" in capsys.readouterr().out


def test_cli_facet_with_dry_run_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "load_payers", lambda: [{"id": "humana", "base_url": "u"}])
    with pytest.raises(SystemExit):
        harvest_fhir.main(["harvest_fhir", "humana", "--facet", "state", "--values", "CA", "--dry-run"])


def test_shard_values_parses_inline_and_file(tmp_path):
    assert harvest_fhir._shard_values("CA, NY ,TX", None) == ["CA", "NY", "TX"]
    f = tmp_path / "states.txt"
    f.write_text("CA\nNY,TX\n\n", encoding="utf-8")
    assert harvest_fhir._shard_values(None, str(f)) == ["CA", "NY", "TX"]
    with pytest.raises(SystemExit):
        harvest_fhir._shard_values(None, None)
