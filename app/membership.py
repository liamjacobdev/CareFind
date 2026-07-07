"""Membership store — verified insurance as an instant local set-membership test.

This is the architectural inversion at the core of the rebuild. The old model asked a
payer's FHIR directory, live, once per NPI, at search time (~4.3s / 50 NPIs), so verified
payers had to be gated OFF by default. Here, each verified payer's entire in-network NPI
set is harvested *offline* into a compact Roaring bitmap; at serve time "is this provider
in payer P's network?" is an O(1) membership test against a memory-mapped bitmap — no
network, no per-NPI round-trip. Verified payers become ON by default and nationwide, and
coverage grows by harvesting more payers offline, not by adding live calls.

Storage: one `payers/<id>.roaring` blob per payer + a `manifest.json` carrying each
payer's provenance (source_url, fetched_at) and metadata. NPIs are stored as
`npi - OFFSET` so a current 10-digit NPI lands in uint32 (Roaring's domain); millions of
NPIs compress to single-digit MB, so 20-30 payers sit well under a serverless size limit.
Blobs are mmap'd read-only at serve time (no gzip-into-/tmp inflate).

Trust invariants preserved here:
  • Only a Luhn-valid NPI (see app/npi.py) is ever admitted to a bitmap — a garbage NPI
    can't fabricate a "yes".
  • A payer whose blob is missing/corrupt loads as ABSENT (its source answers "unknown"),
    never as "everyone is out of network".
  • Staleness is explicit: an entry older than its SLO is flagged `stale` so a serve layer
    can demote it from a fresh green to "data confirmed <date>" — never silently served as
    fresh, and never flipped to a "no".
"""
from __future__ import annotations

import hashlib
import json
import logging
import mmap
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pyroaring import BitMap, FrozenBitMap

from .npi import luhn_valid

log = logging.getLogger("innetwork.membership")

# NPIs are 10 digits beginning with 1 or 2 (NPPES), so `npi - OFFSET` is in
# [3_000_000, 1_999_999_999] — comfortably inside uint32, which Roaring requires. Storing
# the offset in the manifest (not just as a constant) means a future NPI-range change is a
# rebuild, not a silent corruption.
OFFSET = 1_000_000_000
_UINT32_MAX = 0xFFFFFFFF

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

# Default staleness budget (days) for a harvested payer. The plan's mandate: past this,
# demote from fresh-green to "data confirmed <date>" rather than serve a stale green.
# Medicare (quarterly) overrides this with a longer budget via the builder.
DEFAULT_MAX_AGE_DAYS = 45


def encode(npi: str) -> int | None:
    """Map an NPI string to its bitmap key (`npi - OFFSET`), or None if it is not a
    valid, in-range NPI. The Luhn gate here is what keeps a fabricated NPI out of a
    verified set — on both the build side (admission) and the query side (a malformed
    lookup NPI can never match)."""
    if not luhn_valid(npi):
        return None
    v = int(npi) - OFFSET
    return v if 0 <= v <= _UINT32_MAX else None


@dataclass(frozen=True)
class ManifestEntry:
    """One payer's provenance + metadata. `level` mirrors the trust model: "plan" (e.g.
    Medicare — a hit is plan-level confirmation) vs "payer" (listed in a payer's network
    file — confirm the specific plan). `method` records how the set was harvested."""
    id: str
    label: str
    category: str
    level: str                       # "plan" | "payer"
    method: str                      # "cms-enrollment" | "fhir-plannet" | "tic"
    source_url: str
    fetched_at: float                # epoch seconds
    count: int
    file: str                        # blob filename, relative to the store root
    sha256: str
    states: list[str] | None = None  # None -> national; a list -> regional
    max_age_days: int = DEFAULT_MAX_AGE_DAYS

    def is_stale(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.fetched_at) > self.max_age_days * 86400.0

    def age_days(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return (now - self.fetched_at) / 86400.0


# ── Build side (offline: CLI / CI harvest) ────────────────────────────────────
def build_bitmap(npis: Iterable[str]) -> tuple[BitMap, int, int]:
    """Luhn-filter + offset-encode `npis` into a Roaring bitmap.

    Returns (bitmap, admitted, rejected). `rejected` counts values that failed the NPI
    check digit or fell out of uint32 range — surfaced by the builder so a harvest that
    silently drops most of its input (a parser bug, a wrong field) is loud, not invisible.
    """
    bm = BitMap()
    admitted = rejected = 0
    for n in npis:
        v = encode(str(n).strip())
        if v is None:
            rejected += 1
            continue
        bm.add(v)
        admitted += 1
    return bm, admitted, rejected


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (temp in the same dir, then replace) so a reader
    never sees a half-written blob/manifest and a crashed build never corrupts the store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_payer(
    root: Path,
    *,
    id: str,
    label: str,
    category: str,
    level: str,
    method: str,
    source_url: str,
    states: list[str] | None,
    bitmap: BitMap,
    fetched_at: float | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> ManifestEntry:
    """Serialize one payer's bitmap to `root/<id>.roaring`, then upsert its manifest entry.

    The blob is written first and hashed; the manifest (the index a serve loads) is
    rewritten last, so a reader either sees the old consistent state or the new one.
    """
    root = Path(root)
    blob = bitmap.serialize()
    fname = f"{id}.roaring"
    _atomic_write(root / fname, blob)
    entry = ManifestEntry(
        id=id, label=label, category=category, level=level, method=method,
        source_url=source_url, fetched_at=time.time() if fetched_at is None else fetched_at,
        count=len(bitmap), file=fname, sha256=_sha256(blob), states=states,
        max_age_days=max_age_days,
    )
    _upsert_manifest(root, entry)
    return entry


def _read_manifest_dict(root: Path) -> dict[str, Any]:
    path = Path(root) / MANIFEST_NAME
    if not path.exists():
        return {"version": MANIFEST_VERSION, "offset": OFFSET, "payers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("payers"), dict):
            return data
    except Exception as e:
        log.warning("Unreadable manifest at %s (%s: %s); starting fresh", path, type(e).__name__, e)
    return {"version": MANIFEST_VERSION, "offset": OFFSET, "payers": {}}


def _upsert_manifest(root: Path, entry: ManifestEntry) -> None:
    data = _read_manifest_dict(root)
    data["version"] = MANIFEST_VERSION
    data["offset"] = OFFSET
    data["generated_at"] = time.time()
    data["payers"][entry.id] = asdict(entry)
    _atomic_write(Path(root) / MANIFEST_NAME,
                  (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


# ── Serve side (read-only, mmap'd) ────────────────────────────────────────────
@dataclass
class _Loaded:
    entry: ManifestEntry
    bitmap: FrozenBitMap
    _mm: Any = field(repr=False, default=None)   # kept alive so the mmap isn't GC'd


class MembershipStore:
    """Read-only view over a `payers/` directory: the manifest + one mmap'd Roaring bitmap
    per payer. Load once at startup; every membership test after that is a local, ~150ns
    set lookup — no network, no per-NPI round-trip."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._loaded: dict[str, _Loaded] = {}
        self.offset = OFFSET

    def load(self) -> int:
        """Load (or reload) every payer named in the manifest. A payer whose blob is
        missing, size-mismatched, or corrupt is skipped with a warning — it becomes
        "unknown", never a false "not in network". Returns the number of payers loaded."""
        self.close()
        data = _read_manifest_dict(self.root)
        self.offset = int(data.get("offset", OFFSET))
        for pid, raw in (data.get("payers") or {}).items():
            try:
                entry = _entry_from_dict(raw)
            except Exception as e:
                log.warning("Skipping malformed manifest entry %r: %s: %s", pid, type(e).__name__, e)
                continue
            blob_path = self.root / entry.file
            if not blob_path.exists():
                log.warning("Payer %r blob missing (%s) — skipped (serves as unknown)", pid, blob_path)
                continue
            try:
                loaded = self._load_blob(entry, blob_path)
            except Exception as e:
                log.warning("Payer %r blob unreadable (%s) — skipped: %s: %s",
                            pid, blob_path, type(e).__name__, e)
                continue
            self._loaded[pid] = loaded
        log.info("MembershipStore loaded %d payer(s) from %s", len(self._loaded), self.root)
        return len(self._loaded)

    def _load_blob(self, entry: ManifestEntry, blob_path: Path) -> _Loaded:
        fh = open(blob_path, "rb")
        try:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        finally:
            # The mmap keeps its own handle to the underlying file on both platforms;
            # closing the Python file object is safe once the mapping exists.
            fh.close()
        buf = memoryview(mm)
        # Verify the blob matches the manifest hash: a truncated/altered bitmap must not
        # silently answer membership. On mismatch we refuse the payer (caller logs/skip).
        if entry.sha256 and _sha256(bytes(buf)) != entry.sha256:
            mm.close()
            raise ValueError("sha256 mismatch (blob does not match manifest)")
        bitmap = FrozenBitMap.deserialize(bytes(buf))
        # We copied into FrozenBitMap via bytes(); the mmap is no longer strictly needed,
        # but keeping it referenced is cheap and future-proofs a true zero-copy path.
        return _Loaded(entry=entry, bitmap=bitmap, _mm=mm)

    def close(self) -> None:
        for l in self._loaded.values():
            mm = l._mm
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
        self._loaded = {}

    # -- introspection --
    def payers(self) -> list[ManifestEntry]:
        return [l.entry for l in self._loaded.values()]

    def entry(self, payer_id: str) -> ManifestEntry | None:
        l = self._loaded.get(payer_id)
        return l.entry if l else None

    def loaded(self, payer_id: str) -> bool:
        return payer_id in self._loaded

    def count(self, payer_id: str) -> int:
        l = self._loaded.get(payer_id)
        return len(l.bitmap) if l else 0

    # -- membership --
    def has(self, payer_id: str, npi: str) -> bool:
        """True iff `npi` is in `payer_id`'s in-network set. A payer that isn't loaded, or
        a non-NPI query value, returns False (the source layer maps not-loaded to
        "unknown"; a malformed NPI genuinely isn't in any validated set)."""
        l = self._loaded.get(payer_id)
        if l is None:
            return False
        v = encode(npi)
        return v is not None and v in l.bitmap

    def has_many(self, payer_id: str, npis: Iterable[str]) -> set[str]:
        """The subset of `npis` present in `payer_id`'s set (empty if not loaded)."""
        l = self._loaded.get(payer_id)
        if l is None:
            return set()
        bm = l.bitmap
        out: set[str] = set()
        for n in npis:
            v = encode(n)
            if v is not None and v in bm:
                out.add(str(n))
        return out


def _entry_from_dict(raw: dict[str, Any]) -> ManifestEntry:
    """Build a ManifestEntry from a manifest dict, ignoring unknown keys (forward-compat)."""
    fields = ManifestEntry.__dataclass_fields__
    return ManifestEntry(**{k: v for k, v in raw.items() if k in fields})
