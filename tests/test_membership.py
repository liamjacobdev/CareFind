"""The membership store — the rebuilt verified tier. These tests hold it to the same bar
as tests/test_trust_rules.py: a hit is a real, provenanced, verified in-network answer;
absence is a genuine "no" (the bitmap is the complete harvested set); a not-loaded payer
is "unknown", never a fabricated "no"; a garbage NPI can't fabricate a "yes"; and a stale
payer is flagged, never silently served as a fresh green."""
import time

import pytest
from pyroaring import BitMap

from app import membership
from app.config import settings
from app.insurance import MembershipSource, Registry

# Real, valid NPIs (pass the Luhn gate). "PRESENT" get added to the test bitmap; "ABSENT"
# is valid but deliberately not added, so it exercises the genuine-False path.
PRESENT = ["1003000126", "1003000134", "1003000142"]
ABSENT = "1992999874"
BOGUS = "0000000000"  # fails Luhn — must never match


def _write(root, npis, **kw):
    """Build a bitmap from `npis` and write it as a payer, returning the ManifestEntry."""
    bm, _admitted, _rejected = membership.build_bitmap(npis)
    kw.setdefault("id", "cigna")
    kw.setdefault("label", "Cigna")
    kw.setdefault("category", "commercial")
    kw.setdefault("level", "payer")
    kw.setdefault("method", "fhir-plannet")
    kw.setdefault("source_url", "https://cigna.example/directory")
    kw.setdefault("states", None)
    return membership.write_payer(root, bitmap=bm, **kw)


def _store(root):
    s = membership.MembershipStore(root)
    s.load()
    return s


# ── encode / build: the Luhn admission gate ───────────────────────────────────
def test_encode_offsets_valid_and_rejects_garbage():
    v = membership.encode("1003000126")
    assert v == 1003000126 - membership.OFFSET
    assert 0 <= v <= 0xFFFFFFFF
    for bad in [BOGUS, "1234567890", "123", "", "abcdefghij"]:
        assert membership.encode(bad) is None


def test_build_bitmap_counts_rejections():
    bm, admitted, rejected = membership.build_bitmap(PRESENT + [BOGUS, "1234567890", "x"])
    assert admitted == len(PRESENT)
    assert rejected == 3
    assert len(bm) == len(PRESENT)


# ── store roundtrip + membership ──────────────────────────────────────────────
def test_roundtrip_membership(tmp_path):
    entry = _write(tmp_path, PRESENT)
    assert entry.count == len(PRESENT)
    assert entry.sha256 and (tmp_path / entry.file).exists()

    s = _store(tmp_path)
    assert s.loaded("cigna") and s.count("cigna") == len(PRESENT)
    for n in PRESENT:
        assert s.has("cigna", n) is True
    assert s.has("cigna", ABSENT) is False   # complete set -> absence is a genuine no
    assert s.has("cigna", BOGUS) is False     # a non-NPI query can't match
    assert s.has("unknown_payer", PRESENT[0]) is False
    got = s.has_many("cigna", PRESENT + [ABSENT, BOGUS])
    assert got == set(PRESENT)


def test_manifest_entry_is_forward_compatible():
    raw = {"id": "x", "label": "X", "category": "commercial", "level": "payer",
           "method": "tic", "source_url": "u", "fetched_at": 1.0, "count": 0,
           "file": "x.roaring", "sha256": "", "states": None,
           "future_field_we_dont_know": 42}  # unknown keys must be ignored, not crash
    e = membership._entry_from_dict(raw)
    assert e.id == "x" and e.method == "tic"


# ── failure modes never fabricate a "no" ──────────────────────────────────────
def test_missing_blob_is_skipped_not_fatal(tmp_path):
    _write(tmp_path, PRESENT)
    (tmp_path / "cigna.roaring").unlink()   # blob gone, manifest still references it
    s = _store(tmp_path)
    assert not s.loaded("cigna")             # unknown, not "everyone out of network"
    assert s.has("cigna", PRESENT[0]) is False
    assert s.has_many("cigna", PRESENT) == set()


def test_corrupt_blob_is_refused(tmp_path):
    _write(tmp_path, PRESENT)
    (tmp_path / "cigna.roaring").write_bytes(b"not a roaring bitmap")  # sha256 mismatch
    s = _store(tmp_path)
    assert not s.loaded("cigna")


# ── staleness: flagged, never a silent stale green ────────────────────────────
def test_staleness_flag(tmp_path):
    fresh = _write(tmp_path, PRESENT, id="fresh", fetched_at=time.time(), max_age_days=45)
    stale = _write(tmp_path, PRESENT, id="stale",
                   fetched_at=time.time() - 100 * 86400, max_age_days=45)
    assert fresh.is_stale() is False
    assert stale.is_stale() is True
    assert stale.age_days() > 45


# ── MembershipSource: verified, instant, provenanced, on by default ───────────
@pytest.mark.asyncio
async def test_source_trust_semantics(tmp_path):
    _write(tmp_path, PRESENT, id="cigna")
    s = _store(tmp_path)
    src = MembershipSource(s.entry("cigna"), s)
    assert src.confidence == "verified"
    assert src.requires_network is False          # the whole point: no live call
    assert src.available()

    out = await src.check_many(PRESENT + [ABSENT])
    assert all(out[n] is True for n in PRESENT)
    assert out[ABSENT] is False

    prov = src.provenance_many(PRESENT)
    for n in PRESENT:
        assert prov[n]["source_url"] == "https://cigna.example/directory"
        assert prov[n]["fetched_at"] and prov[n]["stale"] is False


@pytest.mark.asyncio
async def test_source_regional_scoping_yields_unknown_out_of_state(tmp_path):
    _write(tmp_path, PRESENT, id="excellus", states=["NY"])
    s = _store(tmp_path)
    src = MembershipSource(s.entry("excellus"), s)
    out = await src.check_many_ctx({PRESENT[0]: {"state": "TX"}})
    assert out[PRESENT[0]] is None                 # out of scope -> unknown, never fabricated
    out = await src.check_many_ctx({PRESENT[0]: {"state": "NY"}})
    assert out[PRESENT[0]] is True


@pytest.mark.asyncio
async def test_not_loaded_source_answers_unknown(tmp_path):
    _write(tmp_path, PRESENT, id="cigna")
    s = _store(tmp_path)
    s.close()                                       # release the mmap so the blob unlinks
    (tmp_path / "cigna.roaring").unlink()
    s.load()                                        # reload: blob gone
    # A source pointed at a now-unloaded payer must answer None, never False.
    entry = membership.ManifestEntry(
        id="cigna", label="Cigna", category="commercial", level="payer", method="fhir-plannet",
        source_url="u", fetched_at=time.time(), count=0, file="cigna.roaring", sha256="")
    src = MembershipSource(entry, s)
    assert not src.available()
    out = await src.check_many(PRESENT)
    assert all(out[n] is None for n in PRESENT)


# ── Registry integration: harvested payer is verified-by-default, supersedes legacy ──
@pytest.mark.asyncio
async def test_membership_medicare_supersedes_legacy_and_is_on_by_default(tmp_path, temp_db):
    _write(tmp_path, PRESENT, id="medicare", label="Medicare (Original)",
           category="medicare", level="plan", method="cms-enrollment",
           source_url="https://data.cms.gov/enrollment")
    old_dir, old_use = settings.membership_dir, settings.use_membership
    settings.membership_dir, settings.use_membership = str(tmp_path), True
    try:
        reg = Registry()
        reg.build()
        medicare_sources = [s for s in reg.sources if s.id == "medicare"]
        # Exactly one medicare source, and it is the membership one (legacy superseded).
        assert len(medicare_sources) == 1
        assert isinstance(medicare_sources[0], MembershipSource)
        assert medicare_sources[0].requires_network is False

        # Verified-by-default: annotate WITHOUT `only` (an unfiltered search) still returns
        # the verified medicare answer, with provenance — no live call, nationwide.
        ann = await reg.annotate([{"npi": PRESENT[0], "stateAb": "CA"}])
        info = ann[PRESENT[0]]["medicare"]
        assert info["value"] is True and info["confidence"] == "verified"
        assert info["source_url"] and info["fetched_at"]
    finally:
        settings.membership_dir, settings.use_membership = old_dir, old_use
        if reg.membership_store:
            reg.membership_store.close()
