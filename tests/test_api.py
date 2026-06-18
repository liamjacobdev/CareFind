"""End-to-end HTTP tests with a mocked registry (no live NPPES/Nominatim)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, main, metrics

_GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "normalize_golden.json").read_text(encoding="utf-8")
)

# Two NPPES-shaped records: one enrolled in Medicare, one not.
CANNED = [
    {"number": "1003000126", "enumeration_type": "NPI-1",
     "basic": {"first_name": "John", "last_name": "Smith", "status": "A"},
     "addresses": [{"address_purpose": "LOCATION", "address_1": "1 Main St",
                    "city": "Crestview", "state": "FL", "postal_code": "32536"}],
     "taxonomies": [{"desc": "Family Medicine", "primary": True}]},
    {"number": "9999999999", "enumeration_type": "NPI-1",
     "basic": {"first_name": "Jane", "last_name": "Doe", "status": "A"},
     "addresses": [{"address_purpose": "LOCATION", "address_1": "2 Oak St",
                    "city": "Crestview", "state": "FL", "postal_code": "32536"}],
     "taxonomies": [{"desc": "Family Medicine", "primary": True}]},
]


@pytest.fixture()
def client(temp_db, monkeypatch):
    db.medicare_add_many(["1003000126"])

    async def fake_search(q):
        return CANNED

    async def fake_batch(items, budget_seconds=None):
        return {}

    monkeypatch.setattr(main.nppes, "search", fake_search)
    monkeypatch.setattr(main.geocode, "geocode_batch", fake_batch)
    with TestClient(main.app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["medicare_npis"] >= 1
    assert body["ok"] is True
    assert "data_freshness" in body


def test_readyz_reports_ready(client):
    """D4 readiness: datastore reachable + registry built."""
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"] is True


def test_healthz_flips_to_503_when_a_source_is_stale(client):
    """C3 dead-man's-switch: a tracked source past its SLO flips /healthz unhealthy."""
    import time as _t
    # Fresh ingest -> healthy.
    db.source_meta_set("medicare", "https://cms.example/file", _t.time())
    assert client.get("/healthz").status_code == 200

    # Older than the Medicare SLO -> stale -> 503, and named in the freshness report.
    stale_ts = _t.time() - (main.settings.medicare_max_age_days + 5) * 86400
    db.source_meta_set("medicare", "https://cms.example/file", stale_ts)
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "medicare" in body["data_freshness"]["stale"]


def test_admin_ingest_requires_token(client, monkeypatch):
    # Disabled (no token configured) -> 404.
    monkeypatch.setattr(main.settings, "admin_token", "")
    assert client.post("/admin/ingest?source=tic").status_code == 404

    # Configured -> wrong/missing token rejected, correct token schedules the ingest.
    monkeypatch.setattr(main.settings, "admin_token", "s3cret")
    called = {}
    monkeypatch.setattr(main, "_trigger_ingest", lambda source: called.setdefault("source", source))
    assert client.post("/admin/ingest?source=tic").status_code == 403
    assert client.post("/admin/ingest?source=tic",
                       headers={"Authorization": "Bearer wrong"}).status_code == 403
    r = client.post("/admin/ingest?source=tic", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
    assert called["source"] == "tic"  # background task ran after the response


def test_plans_grouped(client):
    data = client.get("/api/insurance/plans").json()
    assert "categories" in data and "plans" in data
    cat_ids = {c["id"] for c in data["categories"]}
    assert {"medicare", "commercial"} <= cat_ids


def test_normalize_matches_golden(temp_db):
    """T1.5 (Python half): the backend normalize() produces the shared structural
    fields the frontend buildProviders() must also produce. The JS half asserts the
    same fixture in tests-js/parity.test.js, so renaming a field on either side fails
    CI. Phone/fax are excluded (backend raw, frontend formats downstream)."""
    out = main.normalize(_GOLDEN["record"])
    for key, val in _GOLDEN["expected"].items():
        assert out[key] == val, f"normalize() field {key!r} drifted from the golden fixture"


def test_frontend_logic_js_served(client):
    """The page loads its pure logic from /carefind.logic.js — the backend must
    serve it as JavaScript (and it must be the real, extracted module)."""
    r = client.get("/carefind.logic.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "function buildProviders" in r.text


def test_request_id_header_and_metrics(client):
    """T5.2: every response carries an X-Request-ID, and /metrics reflects request
    counts, status breakdown, FHIR cache hit rate, and upstream errors."""
    metrics.reset()
    r = client.get("/api/insurance/plans")
    assert r.headers.get("x-request-id")  # correlation id echoed

    # A real search exercises the FHIR cache + geocode paths so the rates populate.
    client.get("/api/providers/search?zip=32536&geocode=false")

    m = client.get("/metrics").json()
    assert m["requests_total"] >= 2
    assert "200" in m["requests_by_status"]
    assert "geocode_cache" in m and "fhir_cache" in m
    assert m["upstream_errors"] == 0


def test_bundle_served_and_page_has_no_inline_logic(client):
    """B1: the page loads its logic from the built bundle (a single same-origin
    script), injects config as data, and carries NO inline business logic — the
    precondition for dropping 'unsafe-inline' from the script CSP (D3)."""
    page = client.get("/").text
    assert 'src="carefind.bundle.js"' in page
    # Config is external (D3) — referenced, not inlined — so the HTML has no inline JS.
    assert 'src="carefind.config.js"' in page
    assert "window.CAREFIND_CONFIG" not in page
    # The old inline app functions must be gone from the HTML (now in the bundle).
    assert "function handleSearch" not in page
    assert "function bootstrap" not in page

    bundle = client.get("/carefind.bundle.js")
    assert bundle.status_code == 200
    assert "javascript" in bundle.headers["content-type"]
    assert "buildProviders" in bundle.text             # logic bundled in
    assert "addEventListener" in bundle.text           # app code bundled in


def test_security_headers_and_strict_csp(client):
    """D3: full security-header set on every response; the page's CSP forbids inline
    script (no 'unsafe-inline' in script-src) and loads config + app as external JS."""
    r = client.get("/")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert "strict-origin" in h["Referrer-Policy"]
    assert "geolocation=(self)" in h["Permissions-Policy"]
    assert "max-age=" in h["Strict-Transport-Security"]

    page = r.text
    # The CSP meta drops 'unsafe-inline' from script-src.
    csp = page.split('Content-Security-Policy" content="', 1)[1].split('" />', 1)[0]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    # No inline executable <script> blocks remain; config + app are external.
    assert "window.CAREFIND_CONFIG" not in page
    assert 'src="carefind.config.js"' in page and 'src="carefind.bundle.js"' in page

    cfg = client.get("/carefind.config.js")
    assert cfg.status_code == 200 and "window.CAREFIND_CONFIG" in cfg.text


def test_metrics_protected_by_admin_token(client, monkeypatch):
    # Open in dev (no token configured).
    monkeypatch.setattr(main.settings, "admin_token", "")
    assert client.get("/metrics").status_code == 200
    # Protected once a token is configured.
    monkeypatch.setattr(main.settings, "admin_token", "s3cret")
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_no_pii_in_logs(client, monkeypatch, caplog):
    """D3 gate: search terms, the upstream URL, and client IPs must not be persisted to
    logs. The failure log records only which fields were present + the error type."""
    async def boom(q):
        # An httpx-style error whose message embeds the URL+query — must NOT be logged.
        raise RuntimeError("GET https://npiregistry.cms.hhs.gov/api/?postal_code=90210&last_name=Secretpatient failed")
    monkeypatch.setattr(main.nppes, "search", boom)

    with caplog.at_level("INFO"):
        r = client.get("/api/providers/search", params={"zip": "90210", "name": "Secretpatient"})
    assert r.status_code == 502
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "90210" not in blob          # the searched ZIP is not persisted
    assert "Secretpatient" not in blob  # the searched name is not persisted
    assert "npiregistry.cms.hhs.gov/api/?" not in blob  # nor the upstream URL/query

    # Rate-limit log must not persist the client IP.
    caplog.clear()
    monkeypatch.setattr(main.settings, "trust_proxy", True)
    monkeypatch.setattr(main.settings, "rate_limit_max", 1)
    main._limiter.reset()
    h = {"X-Forwarded-For": "203.0.113.77"}
    with caplog.at_level("WARNING"):
        client.get("/api/insurance/plans", headers=h)
        client.get("/api/insurance/plans", headers=h)  # trips the limit
    assert "203.0.113.77" not in "\n".join(rec.getMessage() for rec in caplog.records)


def test_pwa_assets_served(client):
    """D1: the PWA shell — manifest, service worker, icon — is served so the app is
    installable and works offline. The page links the manifest and theme-color."""
    page = client.get("/").text
    assert 'rel="manifest"' in page and 'name="theme-color"' in page

    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and "manifest" in m.headers["content-type"]
    assert m.json()["start_url"] == "/" and m.json()["display"] == "standalone"

    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert "addEventListener" in sw.text and "carefind-shell" in sw.text

    icon = client.get("/carefind-icon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg")


@pytest.mark.parametrize("path", ["/", "/carefind.logic.js", "/carefind.bundle.js"])
def test_static_files_etag_304(client, path):
    """T4.2: static frontend files carry an ETag + Cache-Control, and a repeat load
    with If-None-Match returns 304 (not re-downloaded)."""
    r1 = client.get(path)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag and "cache-control" in r1.headers
    r2 = client.get(path, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers.get("etag") == etag


def test_search_attaches_confidence_shape(client):
    data = client.get("/api/providers/search?zip=32536&geocode=false").json()
    assert data["count"] == 2
    by_npi = {p["npi"]: p for p in data["providers"]}
    assert by_npi["1003000126"]["insurance"]["medicare"]["value"] is True
    assert by_npi["1003000126"]["insurance"]["medicare"]["confidence"] == "verified"
    assert by_npi["9999999999"]["insurance"]["medicare"]["value"] is False


def test_verified_filter_excludes_non_enrolled(client):
    data = client.get(
        "/api/providers/search?zip=32536&geocode=false&accepts=medicare&accepts_mode=verified"
    ).json()
    assert [p["npi"] for p in data["providers"]] == ["1003000126"]


def test_verified_mode_excludes_estimated_only_payer(client):
    # aetna has no verified source here, so verified-mode yields nobody...
    v = client.get("/api/providers/search?zip=32536&geocode=false&accepts=aetna&accepts_mode=verified").json()
    assert v["count"] == 0


def test_national_estimate_does_not_filter(client):
    """A4: selecting a national estimate ('aetna') doesn't narrow the result set —
    it's area context, not a provider-specific filter. The response says so via
    applied_filters (empty) and context_plans, instead of presenting all in-state
    providers as a filtered match."""
    a = client.get(
        "/api/providers/search?zip=32536&geocode=false&accepts=aetna&accepts_mode=any"
    ).json()
    assert a["count"] == 2                  # unchanged from an unfiltered search
    assert a["applied_filters"] == []       # nothing actually filtered
    assert a["context_plans"] == ["aetna"]  # surfaced as context only


def test_regional_estimate_still_discriminates_by_state(client):
    """A4: a regional estimate genuinely filters — Kaiser doesn't serve FL, so an
    'any'-mode Kaiser filter on FL providers excludes everyone (it discriminates)."""
    k = client.get(
        "/api/providers/search?zip=32536&geocode=false&accepts=kaiser&accepts_mode=any"
    ).json()
    assert k["applied_filters"] == ["kaiser"]  # it does drive the filter
    assert k["count"] == 0                      # and excludes the FL providers


def test_normalize_pins_provider_shape():
    """Golden record for normalize(). carefind.html:buildProviders() mirrors this
    field-for-field for the standalone path; if you add/rename a field here, update
    the frontend (and this assertion) so the two shapes can't silently drift."""
    rec = {
        "number": "1003000126", "enumeration_type": "NPI-1",
        "basic": {"first_name": "John", "last_name": "Smith", "credential": "M.D.",
                  "gender": "M", "status": "A", "enumeration_date": "2007-05-23",
                  "last_updated": "2020-01-01", "sole_proprietor": "NO"},
        "addresses": [
            {"address_purpose": "LOCATION", "address_1": "1 main st", "city": "crestview",
             "state": "FL", "postal_code": "325361234", "telephone_number": "850-555-0100",
             "fax_number": "850-555-0101"},
            {"address_purpose": "MAILING", "address_1": "PO Box 9", "city": "crestview",
             "state": "FL", "postal_code": "32536"},
        ],
        "taxonomies": [{"desc": "Family Medicine", "code": "207Q00000X", "primary": True,
                        "state": "FL", "license": "ME123"}],
    }
    assert main.normalize(rec) == {
        "npi": "1003000126", "name": "John Smith, M.D", "isOrg": False,
        "specialty": "Family Medicine",
        "taxonomies": [{"desc": "Family Medicine", "code": "207Q00000X", "primary": True,
                        "state": "FL", "license": "ME123"}],
        "address1": "1 Main St", "city": "Crestview", "stateAb": "FL", "postalCode": "32536",
        "fullAddress": "1 Main St, Crestview, FL, 32536",
        "mailingAddress": "Po Box 9, Crestview, FL, 32536",
        "phone": "850-555-0100", "fax": "850-555-0101", "gender": "Male",
        "soleProprietor": "NO", "credential": "M.D.", "status": "Active",
        "enumerationDate": "2007-05-23", "lastUpdated": "2020-01-01",
        "insurance": {}, "lat": None, "lng": None, "distance": None,
    }


def test_rate_limit_is_per_client_behind_proxy(client, monkeypatch):
    """With trust_proxy on, two clients arriving via the same proxy but with
    different X-Forwarded-For IPs get independent buckets — the proxy's own IP
    no longer collapses everyone into one global limit."""
    monkeypatch.setattr(main.settings, "trust_proxy", True)
    monkeypatch.setattr(main.settings, "rate_limit_max", 3)
    monkeypatch.setattr(main.settings, "rate_limit_window", 60)
    main._limiter.reset()
    h1 = {"X-Forwarded-For": "1.1.1.1"}
    h2 = {"X-Forwarded-For": "2.2.2.2"}
    for _ in range(3):
        assert client.get("/api/insurance/plans", headers=h1).status_code == 200
    assert client.get("/api/insurance/plans", headers=h1).status_code == 429
    # A different client IP still has its full budget.
    assert client.get("/api/insurance/plans", headers=h2).status_code == 200


def test_noop_rate_limiter_swap_disables_limiting(client, monkeypatch):
    """The RateLimiter is a swappable seam: dropping in a no-op limiter removes the
    429 even past the configured max — proving the limiter is injected, not baked in."""
    from app.ratelimit import NoopRateLimiter
    monkeypatch.setattr(main.settings, "rate_limit_max", 1)
    monkeypatch.setattr(main, "_limiter", NoopRateLimiter())
    for _ in range(5):
        assert client.get("/api/insurance/plans").status_code == 200


def test_geocode_batch_rejects_oversized(client):
    over = [{"key": str(i), "q": f"{i} Main St, Crestview, FL"} for i in range(101)]
    assert client.post("/api/geocode/batch", json={"items": over}).status_code == 413
    # The cap boundary (100) is still accepted.
    ok = [{"key": str(i), "q": f"{i} Main St, Crestview, FL"} for i in range(100)]
    assert client.post("/api/geocode/batch", json={"items": ok}).status_code == 200


def test_geocode_batch_passes_budget(client, monkeypatch):
    """A normal batch is bounded by a budget so it can't run unbounded."""
    seen = {}

    async def rec_batch(items, budget_seconds=None):
        seen["budget"] = budget_seconds
        seen["n"] = len(items)
        return {}

    monkeypatch.setattr(main.geocode, "geocode_batch", rec_batch)
    items = [{"key": "a", "q": "1 Main St"}, {"key": "b", "q": "2 Oak St"}]
    assert client.post("/api/geocode/batch", json={"items": items}).status_code == 200
    assert seen == {"budget": 20, "n": 2}


def _rec(npi, last, zip_):
    return {"number": npi, "enumeration_type": "NPI-1",
            "basic": {"first_name": "Test", "last_name": last, "status": "A"},
            "addresses": [{"address_purpose": "LOCATION", "address_1": "1 Main St",
                           "city": "Crestview", "state": "FL", "postal_code": zip_}],
            "taxonomies": [{"desc": "Family Medicine", "primary": True}]}


def test_radius_filters_and_sorts_before_truncation(temp_db, monkeypatch):
    """The regression that shipped the bug: with a widened pool, out-of-radius
    providers must be dropped and the closest must survive truncation to `limit`
    — distance-filtering/sorting happens BEFORE the slice, not after."""
    center = [30.76, -86.57]  # near Crestview, FL
    records = [
        _rec("1000000003", "Farish", "32539"),  # ~22 mi (in radius, but truncated)
        _rec("1000000002", "Out", "30301"),     # Atlanta ~250 mi (out of radius)
        _rec("1000000001", "Near", "32536"),     # ~1 mi
        _rec("1000000004", "Mid", "32567"),      # ~13 mi
    ]
    coords = {
        "1000000001": [30.77, -86.58],   # ~1 mi
        "1000000004": [30.95, -86.50],   # ~13 mi
        "1000000003": [31.05, -86.40],   # ~22 mi
        "1000000002": [33.75, -84.39],   # Atlanta, ~250 mi
    }

    async def fake_search(q):
        return records

    async def fake_batch(items, budget_seconds=None):
        return {it["key"]: coords[it["key"]] for it in items if it["key"] in coords}

    async def fake_one(q):
        return center

    monkeypatch.setattr(main.nppes, "search", fake_search)
    monkeypatch.setattr(main.geocode, "geocode_batch", fake_batch)
    monkeypatch.setattr(main.geocode, "geocode_one", fake_one)

    with TestClient(main.app) as c:
        data = c.get("/api/providers/search?zip=32536&radius=25&geocode=true&limit=2").json()

    providers = data["providers"]
    npis = [p["npi"] for p in providers]
    # Out-of-radius provider dropped entirely.
    assert "1000000002" not in npis
    # The two CLOSEST survive truncation to limit=2 (not an arbitrary pool slice),
    # distance-ascending.
    assert npis == ["1000000001", "1000000004"]
    dists = [p["distance"] for p in providers]
    assert all(d is not None and d <= 25 for d in dists)
    assert dists == sorted(dists)
    # T1.4: truncation is surfaced, not silent. Three providers are within radius;
    # limit=2 returns the closest two and reports the full in-radius total honestly.
    assert data["count"] == 2
    assert data["total"] == 3
    assert data["truncated"] is True
    assert data["pool_capped"] is False  # pool (4) well under the ceiling
