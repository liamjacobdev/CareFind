"""SQLite indexes + geocode cache."""
from app import db, geocode


def test_medicare_add_has_many(temp_db):
    # The db layer stores what it's given (minus blanks); digit validation is the
    # ingest CLI's job. Blank entries are dropped, real NPIs deduped.
    db.medicare_add_many(["1003000126", "1003000134", "1003000126", ""])
    assert db.medicare_count() == 2
    assert db.medicare_has("1003000126")
    assert not db.medicare_has("9999999999")
    assert db.medicare_has_many(["1003000126", "9999999999"]) == {"1003000126"}


def test_tic_is_per_payer(temp_db):
    db.tic_add_many("aetna", ["1003000126", "1003000134"])
    db.tic_add_many("cigna", ["1003000142"])
    assert db.tic_count("aetna") == 2
    assert db.tic_count("cigna") == 1
    assert db.tic_has("aetna", "1003000126")
    assert not db.tic_has("cigna", "1003000126")  # payer-scoped
    assert db.tic_has_many("aetna", ["1003000126", "1003000142"]) == {"1003000126"}


def test_geocode_cache_roundtrip(temp_db):
    assert db.geocode_get("k") is None
    db.geocode_set("k", 30.76, -86.57)
    assert db.geocode_get("k") == [30.76, -86.57]


def test_haversine_known_distance():
    crestview, pensacola = [30.7619, -86.5708], [30.4213, -87.2169]
    d = geocode.haversine_miles(crestview, pensacola)
    assert 40 < d < 50  # ~45 miles
