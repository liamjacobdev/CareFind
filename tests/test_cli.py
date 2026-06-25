"""E1 coverage: the ingest CLIs, the payer verifier (offline), and bounded downloads —
the operator-facing entry points and their error paths."""
import gzip
import json

import httpx
import pytest
import respx

from app import cms_catalog, download, ingest_medicare, ingest_tic, ingest_tic_job, verify_payers
from app.config import settings


# ── ingest_medicare CLI ──────────────────────────────────────────────────────
def test_ingest_medicare_main_local_file(temp_db, capsys):
    ingest_medicare.main(["prog", "sample_medicare.csv"])
    assert "Ingested" in capsys.readouterr().out


def test_ingest_medicare_main_requires_arg():
    with pytest.raises(SystemExit):
        ingest_medicare.main(["prog"])


def test_ingest_medicare_finds_loose_npi_column(temp_db, tmp_path):
    f = tmp_path / "enroll.csv"
    f.write_text("Rendering_NPI,Name\n1003000126,Smith\n9999999999,Doe\n", encoding="utf-8")
    assert ingest_medicare.ingest(str(f)) == 2


def test_ingest_medicare_no_npi_column_errors(temp_db, tmp_path):
    f = tmp_path / "bad.csv"
    f.write_text("Name,City\nSmith,Crestview\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        ingest_medicare.ingest(str(f))


# ── ingest_tic CLI + formats ─────────────────────────────────────────────────
def _innetwork(npis):
    return {"in_network": [{"provider_groups": [{"npi": npis}]}]}


def test_ingest_tic_main_json_file(temp_db, tmp_path, capsys):
    f = tmp_path / "in.json"
    f.write_text(json.dumps(_innetwork(["1003000126", "1112223338"])), encoding="utf-8")
    ingest_tic.main(["prog", "aetna", str(f)])
    assert "Ingested" in capsys.readouterr().out


def test_ingest_tic_main_requires_args():
    with pytest.raises(SystemExit):
        ingest_tic.main(["prog", "aetna"])


def test_ingest_tic_gzip_and_csv_paths(temp_db, tmp_path):
    gz = tmp_path / "in.json.gz"
    gz.write_bytes(gzip.compress(json.dumps(_innetwork(["1003000126"])).encode()))
    assert ingest_tic.ingest("aetna", str(gz)) == 1

    csvf = tmp_path / "npis.csv"
    csvf.write_text("NPI,Name\n1112223338,X\n1999999984,Y\n", encoding="utf-8")
    added = ingest_tic.ingest("cigna", str(csvf))
    assert added == 2


# ── ingest_tic_job CLI ───────────────────────────────────────────────────────
def test_tic_job_main_adhoc_two_arg(temp_db, tmp_path, capsys):
    f = tmp_path / "in.json"
    f.write_text(json.dumps(_innetwork(["1003000126"])), encoding="utf-8")
    ingest_tic_job.main(["prog", "aetna", str(f)])
    assert "aetna" in capsys.readouterr().out


def test_tic_job_run_no_sources_exits(temp_db, tmp_path):
    empty = tmp_path / "none.json"
    empty.write_text(json.dumps({"sources": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        ingest_tic_job.run(sources_path=str(empty))


def test_tic_job_run_unknown_only_payer_exits(temp_db, tmp_path):
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"sources": [{"payer": "aetna", "url": "x"}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        ingest_tic_job.run(only_payer="nobody", sources_path=str(src))


def test_tic_job_load_sources_missing_file_is_empty(tmp_path):
    assert ingest_tic_job.load_sources(str(tmp_path / "absent.json")) == []


def test_tic_job_skips_malformed_source(temp_db, tmp_path, capsys):
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"sources": [{"payer": "aetna"}]}), encoding="utf-8")  # no url
    assert ingest_tic_job.run(sources_path=str(src)) == []
    assert "Skipping malformed" in capsys.readouterr().out


def test_tic_job_run_ingests_local_source(temp_db, tmp_path):
    innet = tmp_path / "in.json"
    innet.write_text(json.dumps(_innetwork(["1003000126", "1112223338"])), encoding="utf-8")
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"sources": [{"payer": "aetna", "url": str(innet)}]}), encoding="utf-8")
    results = ingest_tic_job.run(sources_path=str(src))
    assert results[0]["payer"] == "aetna" and results[0]["added"] == 2


# ── verify_payers offline + rendering ────────────────────────────────────────
def test_verify_payers_offline_and_ledger(tmp_path, monkeypatch):
    results = verify_payers.validate(offline=True)
    assert results and all("status" in r for r in results)
    md = verify_payers.render_ledger(results)
    assert "Provenance ledger" in md
    out = tmp_path / "provenance.md"
    verify_payers.write_ledger(md, out)
    assert out.read_text(encoding="utf-8").startswith("# Provenance ledger")


def test_verify_payers_main_offline_writes_ledger(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "prov.md"
    monkeypatch.setattr(verify_payers, "_LEDGER_PATH", ledger)
    verify_payers.main(["prog", "--offline"])
    assert ledger.exists()
    assert "validated" in capsys.readouterr().out


@respx.mock
def test_verify_payers_probe_validates_and_flags(monkeypatch):
    from app.planet_registry import PlanNetEndpoint
    base = "https://ok.example/r4"
    respx.get(f"{base}/Practitioner").mock(return_value=httpx.Response(200, json={
        "entry": [{"resource": {"resourceType": "Practitioner",
                                "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": "1111111111"}]}}]}))

    def pr(request):
        ident = request.url.params.get("practitioner.identifier", "")
        if ident.endswith("|1111111111"):
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": [
                {"resource": {"resourceType": "PractitionerRole", "active": True,
                              "network": [{"reference": "Network/x"}]}}]})
        return httpx.Response(200, json={"resourceType": "Bundle", "total": 5, "entry": []})

    respx.get(f"{base}/PractitionerRole").mock(side_effect=pr)
    ep = PlanNetEndpoint(id="x", label="X", base_url=base, category="medicaid", states=["MD"])
    with httpx.Client() as c:
        assert verify_payers.probe(c, ep)["status"] == "validated"


@respx.mock
def test_verify_payers_probe_http_error_is_unusable():
    from app.planet_registry import PlanNetEndpoint
    base = "https://err.example/r4"
    respx.get(f"{base}/PractitionerRole").mock(return_value=httpx.Response(500))
    ep = PlanNetEndpoint(id="x", label="X", base_url=base, category="medicaid", states=["MD"])
    with httpx.Client() as c:
        res = verify_payers.probe(c, ep)
    assert res["status"] == "unusable" and "500" in res["error"]


# ── bounded downloads ────────────────────────────────────────────────────────
def test_download_cap_default_and_explicit():
    assert download._cap(None) == settings.ingest_max_bytes
    assert download._cap(123) == 123


@respx.mock
def test_stream_to_bytes_success_under_cap():
    respx.get("https://x.example/f.json").mock(return_value=httpx.Response(200, content=b'{"ok":1}'))
    assert download.stream_to_bytes("https://x.example/f.json") == b'{"ok":1}'


@respx.mock
def test_stream_rejects_declared_content_length_over_cap(monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_bytes", 4)
    respx.get("https://x.example/big").mock(
        return_value=httpx.Response(200, headers={"content-length": "999"}, content=b"xxxxxxxx"))
    with pytest.raises(download.DownloadTooLarge):
        download.stream_to_bytes("https://x.example/big")


@respx.mock
def test_oauth_client_credentials_token_caches_and_sends_basic_auth():
    from app.oauth import ClientCredentials
    route = respx.post("https://idp.example/token").mock(
        return_value=httpx.Response(200, json={"access_token": "TOK1", "expires_in": 3600}))
    cc = ClientCredentials("https://idp.example/token", "cid", "secret", scope="provider")
    with httpx.Client() as c:
        assert cc.token_sync(c) == "TOK1"
        assert cc.token_sync(c) == "TOK1"          # cached -> no second POST
    assert route.call_count == 1
    sent = route.calls[0].request
    import base64
    assert sent.headers["Authorization"] == "Basic " + base64.b64encode(b"cid:secret").decode()
    assert b"grant_type=client_credentials" in sent.content and b"scope=provider" in sent.content


@respx.mock
def test_verify_payers_probe_validates_oauth_gated_endpoint(monkeypatch):
    """An OAuth-gated payer (e.g. Aetna): the validator fetches a token, attaches the
    Bearer, and round-trips by NPI via two_step — exactly the wiring Aetna needs."""
    from app.planet_registry import PlanNetEndpoint
    monkeypatch.setenv("AETNA_ID", "cid")
    monkeypatch.setenv("AETNA_SECRET", "sec")
    base = "https://gated.example/v1/providerdirectorydata"
    respx.post("https://idp.example/token").mock(
        return_value=httpx.Response(200, json={"access_token": "BEARER123", "expires_in": 3600}))

    def needs_bearer(request, ok_body):
        if request.headers.get("Authorization") != "Bearer BEARER123":
            return httpx.Response(401, json={"resourceType": "OperationOutcome"})
        return httpx.Response(200, json=ok_body)

    # head check + Practitioner discovery + two_step
    respx.get(f"{base}/PractitionerRole").mock(side_effect=lambda r: needs_bearer(r, {
        "resourceType": "Bundle", "total": 7, "entry": [
            {"resource": {"resourceType": "PractitionerRole", "active": True,
                          "network": [{"reference": "Network/aetna"}]}}]}))

    def practitioner(request):
        ident = request.url.params.get("identifier", "")
        listed = (not ident) or ident.endswith("|1111111111")
        body = {"resourceType": "Bundle", "entry": (
            [{"resource": {"resourceType": "Practitioner", "id": "p1",
                           "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi",
                                           "value": "1111111111"}]}}] if listed else [])}
        return needs_bearer(request, body)

    respx.get(f"{base}/Practitioner").mock(side_effect=practitioner)

    ep = PlanNetEndpoint(
        id="aetna", label="Aetna", base_url=base, category="commercial", states=None,
        lookup_mode="two_step", auth="oauth2", token_url="https://idp.example/token",
        client_id_env="AETNA_ID", client_secret_env="AETNA_SECRET")
    with httpx.Client() as c:
        assert verify_payers.probe(c, ep)["status"] == "validated"


@respx.mock
def test_latest_medicare_csv_url_resolves_from_catalog():
    """The seed-refresh Action discovers the current quarterly CSV from data.cms.gov."""
    csv_url = "https://data.cms.gov/sites/default/files/2026-05/abc/PPEF_Enrollment_Extract_2026.04.01.csv"
    respx.get(cms_catalog.CATALOG_URL).mock(return_value=httpx.Response(200, json={"dataset": [
        {"title": "Some Other Dataset", "distribution": [{"downloadURL": "https://x/y.csv"}]},
        {"title": "Medicare Fee-For-Service  Public Provider Enrollment",
         "distribution": [
            {"downloadURL": "https://data.cms.gov/x/dictionary.pdf"},
            {"downloadURL": csv_url}]},
    ]}))
    assert cms_catalog.latest_medicare_csv_url() == csv_url


@respx.mock
def test_latest_medicare_csv_url_raises_when_absent():
    respx.get(cms_catalog.CATALOG_URL).mock(
        return_value=httpx.Response(200, json={"dataset": [{"title": "Unrelated"}]}))
    with pytest.raises(LookupError):
        cms_catalog.latest_medicare_csv_url()
