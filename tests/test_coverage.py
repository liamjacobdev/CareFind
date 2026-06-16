"""C4: short-TTL NPPES result cache + verified-coverage-by-state report."""
import httpx
import pytest
import respx

from app import coverage, db, metrics, nppes
from app.config import settings
from app.insurance import Registry


# ── NPPES short-TTL cache ───────────────────────────────────────────────────────
@respx.mock
@pytest.mark.asyncio
async def test_identical_nppes_search_makes_zero_repeat_live_calls():
    nppes._cache.clear()
    metrics.reset()
    route = respx.get(settings.nppes_base).mock(
        return_value=httpx.Response(200, json={"result_count": 1, "results": [{"number": "1003000126"}]}))

    q = {"zip": "32536", "limit": 25}
    first = await nppes.search(q)
    assert route.call_count == 1 and first == [{"number": "1003000126"}]

    second = await nppes.search(dict(q))  # identical query
    assert route.call_count == 1          # served from cache — zero additional live calls
    assert second == first

    snap = metrics.snapshot()["nppes_cache"]
    assert snap["hits"] == 1 and snap["misses"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_nppes_cache_does_not_pin_a_failed_query():
    nppes._cache.clear()
    route = respx.get(settings.nppes_base).mock(return_value=httpx.Response(503))
    q = {"zip": "99999", "limit": 25}
    with pytest.raises(httpx.HTTPStatusError):
        await nppes.search(q)
    # Not cached: a retry still hits the network (the failure wasn't pinned).
    with pytest.raises(httpx.HTTPStatusError):
        await nppes.search(dict(q))
    assert route.call_count >= 2


# ── Verified-coverage-by-state report ────────────────────────────────────────────
def test_coverage_report_scopes_programs_by_state(temp_db, monkeypatch):
    """Medicare (national) covers every state; the validated MD endpoints cover only MD."""
    monkeypatch.setattr(settings, "use_planet_registry", True)
    db.medicare_add_many(["1003000126"])
    reg = Registry()
    reg.build()

    rep = coverage.coverage_report(reg)
    prog_ids = {p["id"] for p in rep["verified_programs"]}
    assert "medicare" in prog_ids
    assert {"priority_partners", "advantage_md"} <= prog_ids  # MD endpoints wired + available

    # Medicare is national; the MD endpoints only appear for MD.
    assert "medicare" in rep["by_state"]["CA"] and "priority_partners" not in rep["by_state"]["CA"]
    assert {"medicare", "priority_partners", "advantage_md"} <= set(rep["by_state"]["MD"])

    assert rep["verified_counts"]["medicare"] == 1
    assert rep["states_with_verified_coverage"] == len(coverage.STATES)  # Medicare everywhere
    # A program scope is reported honestly (national vs the regional state list).
    md = next(p for p in rep["verified_programs"] if p["id"] == "advantage_md")
    assert md["scope"] == ["MD"]
    medi = next(p for p in rep["verified_programs"] if p["id"] == "medicare")
    assert medi["scope"] is None  # national
