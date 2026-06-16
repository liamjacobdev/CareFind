"""B2: the four scale-readiness seams — conformance + swap-ability.

Each external dependency sits behind a Protocol (app/interfaces.py). These tests prove
the shipped $0 implementations satisfy their Protocol AND that an alternate impl can be
swapped in, so scaling later is a config swap, not a rewrite.
"""
from app import cache, db, geocode
from app.cache import InProcessTTLCache, NoopCache, build_cache
from app.interfaces import CacheBackend, Datastore, GeocoderBackend, RateLimiter
from app.ratelimit import InProcessRateLimiter, NoopRateLimiter, build_rate_limiter


# ── Conformance: the shipped defaults satisfy their Protocols ──────────────────
def test_default_impls_conform_to_protocols():
    assert isinstance(build_rate_limiter(), RateLimiter)
    assert isinstance(build_cache(), CacheBackend)
    assert isinstance(db.get_datastore(), Datastore)
    # The geocode module is the GeocoderBackend implementation.
    assert isinstance(geocode, GeocoderBackend)


def test_noop_alternates_also_conform():
    assert isinstance(NoopRateLimiter(), RateLimiter)
    assert isinstance(NoopCache(), CacheBackend)


# ── RateLimiter ────────────────────────────────────────────────────────────────
def test_inprocess_rate_limiter_limits_and_sweeps(monkeypatch):
    """Fixed-window limiting with an injected clock: a key is capped within its
    window, recovers after it, and idle buckets are swept so memory is bounded."""
    monkeypatch.setattr("app.ratelimit.settings.rate_limit_max", 2, raising=False)
    monkeypatch.setattr("app.ratelimit.settings.rate_limit_window", 60, raising=False)
    clock = {"t": 1000.0}
    rl = InProcessRateLimiter(now=lambda: clock["t"])

    assert rl.check("a").allowed is True
    assert rl.check("a").allowed is True
    blocked = rl.check("a")
    assert blocked.allowed is False and blocked.retry_after == 60

    # A different key has its own budget.
    assert rl.check("b").allowed is True
    assert "a" in rl._hits and "b" in rl._hits

    # Past the window, "a" recovers and the long-idle bucket is swept.
    clock["t"] += 200
    assert rl.check("b").allowed is True
    assert "a" not in rl._hits  # swept


def test_rate_limit_max_zero_disables(monkeypatch):
    monkeypatch.setattr("app.ratelimit.settings.rate_limit_max", 0, raising=False)
    rl = InProcessRateLimiter()
    for _ in range(100):
        assert rl.check("x").allowed is True


def test_noop_rate_limiter_never_limits():
    rl = NoopRateLimiter()
    for _ in range(100):
        assert rl.check("x").allowed is True


# ── CacheBackend ────────────────────────────────────────────────────────────────
def test_inprocess_ttl_cache_get_set_and_expiry():
    clock = {"t": 100.0}
    c = InProcessTTLCache(now=lambda: clock["t"])
    assert c.get("k") is None
    c.set("k", {"v": 1}, ttl=10)
    assert c.get("k") == {"v": 1}
    clock["t"] += 11  # past TTL
    assert c.get("k") is None  # expired, evicted on read


def test_inprocess_ttl_cache_respects_soft_cap():
    c = InProcessTTLCache(max_entries=8)
    for i in range(40):
        c.set(f"k{i}", i, ttl=1000)
    assert len(c._store) <= 8  # bounded


def test_noop_cache_never_stores():
    c = NoopCache()
    c.set("k", 1, ttl=1000)
    assert c.get("k") is None


def test_build_cache_selects_from_config(monkeypatch):
    monkeypatch.setattr(cache.settings, "cache_backend", "noop")
    assert isinstance(build_cache(), NoopCache)
    monkeypatch.setattr(cache.settings, "cache_backend", "memory")
    assert isinstance(build_cache(), InProcessTTLCache)


# ── Datastore ────────────────────────────────────────────────────────────────────
def test_datastore_is_swappable(temp_db):
    """The active datastore can be swapped (e.g. for a Postgres impl in D4) and
    restored, without touching call sites."""
    default = db.get_datastore()
    try:
        sentinel = object()

        class FakeDatastore:
            def medicare_count(self):
                return sentinel

        db.use_datastore(FakeDatastore())
        assert db.get_datastore().medicare_count() is sentinel
    finally:
        db.use_datastore(default)
    # Restored to the real SQLite impl.
    assert db.get_datastore().medicare_count() == 0


def test_sqlite_datastore_delegates_to_module(temp_db):
    """The default datastore is the SQLite module path — same answers as the module
    functions the rest of the app calls directly."""
    ds = db.get_datastore()
    db.medicare_add_many(["1003000126"])
    assert ds.medicare_count() == db.medicare_count() == 1
    assert ds.medicare_has("1003000126") is True
