"""End-to-end HTTP tests with a mocked registry (no live NPPES/Nominatim)."""
import pytest
from fastapi.testclient import TestClient

from app import db, main

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
    assert r.json()["medicare_npis"] >= 1


def test_plans_grouped(client):
    data = client.get("/api/insurance/plans").json()
    assert "categories" in data and "plans" in data
    cat_ids = {c["id"] for c in data["categories"]}
    assert {"medicare", "commercial"} <= cat_ids


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
    # ...but 'any' mode accepts the in-state estimate for both.
    a = client.get("/api/providers/search?zip=32536&geocode=false&accepts=aetna&accepts_mode=any").json()
    assert a["count"] == 2


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
    main._hits.clear()
    h1 = {"X-Forwarded-For": "1.1.1.1"}
    h2 = {"X-Forwarded-For": "2.2.2.2"}
    for _ in range(3):
        assert client.get("/api/insurance/plans", headers=h1).status_code == 200
    assert client.get("/api/insurance/plans", headers=h1).status_code == 429
    # A different client IP still has its full budget.
    assert client.get("/api/insurance/plans", headers=h2).status_code == 200


def test_rate_limit_evicts_idle_buckets(client, monkeypatch):
    """Idle buckets are swept so _hits can't grow unbounded across distinct IPs."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(main.settings, "trust_proxy", True)
    monkeypatch.setattr(main.settings, "rate_limit_max", 5)
    monkeypatch.setattr(main.settings, "rate_limit_window", 60)
    monkeypatch.setattr(main, "_last_sweep", 0.0)
    main._hits.clear()

    client.get("/api/insurance/plans", headers={"X-Forwarded-For": "9.9.9.9"})
    assert "9.9.9.9" in main._hits
    clock["t"] += 200  # idle well past the window
    client.get("/api/insurance/plans", headers={"X-Forwarded-For": "8.8.8.8"})
    assert "9.9.9.9" not in main._hits  # swept on the next request
    assert "8.8.8.8" in main._hits


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
