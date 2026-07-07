"""Build harvested membership bitmaps (offline) — the write side of app/membership.py.

Each subcommand harvests one verified payer's in-network NPI set and writes a compact
Roaring bitmap + a manifest entry into the membership dir (default `payers/`). At serve
time InNetwork mmaps those blobs and answers insurance as an instant local set-membership
test — no live per-NPI calls.

This module is the reusable primitive the later rails plug into:
  • Phase 1  — `medicare`: port the CMS Fee-For-Service enrollment set into a bitmap.
  • Phase 2  — a FHIR-bulk harvester feeds `write_payer(... method="fhir-plannet")`.
  • Phase 3  — a TiC harvester feeds `write_payer(... method="tic")`.

Every NPI is Luhn-validated before admission (app/npi.py), so a harvest can never
fabricate a "yes" from a garbage NPI. A harvest that admits far fewer NPIs than it read
is reported loudly (the rejected count), so a parser/field bug is visible, not silent.

Usage:
    python -m app.build_membership medicare                 # port from the local sqlite index
    python -m app.build_membership medicare <csv-or-url>    # (re)build from the CMS file
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from . import db, membership
from .config import settings
from .ingest_medicare import CMS_ENROLLMENT_URL


def _medicare_npis_from_db() -> tuple[Iterator[str], str, float]:
    """Stream every NPI in the local Medicare sqlite index, plus its recorded provenance
    (source_url, fetched_at) so the bitmap inherits the same traceability the sqlite
    source had. Raises if the index is empty (nothing to port)."""
    n = db.medicare_count()
    if n == 0:
        raise SystemExit("Medicare index is empty — run `python -m app.ingest_medicare "
                         "<csv-or-url>` first, or pass a CSV/URL to build from.")
    meta = db.source_meta_get("medicare")
    source_url = meta[0] if meta else CMS_ENROLLMENT_URL
    fetched_at = meta[1] if meta else time.time()

    def _iter() -> Iterator[str]:
        conn = sqlite3.connect(settings.db_path)
        try:
            for (npi,) in conn.execute("SELECT npi FROM medicare"):
                yield npi
        finally:
            conn.close()

    return _iter(), source_url, fetched_at


def build_medicare(src: str | None, root: Path) -> membership.ManifestEntry:
    """Build (or refresh) the Medicare membership bitmap.

    With no `src`, port the existing local sqlite index (fast, and preserves its recorded
    provenance). With a `src` CSV/URL, ingest it into sqlite first (reusing the hardened
    CMS parser) then port — so the on-disk index and the bitmap stay in lock-step.
    """
    if src:
        from . import ingest_medicare
        added = ingest_medicare.ingest(src)
        print(f"Ingested {added} NPIs into the sqlite index from {src}.", flush=True)
    npis, source_url, fetched_at = _medicare_npis_from_db()

    bitmap, admitted, rejected = membership.build_bitmap(npis)
    entry = membership.write_payer(
        root,
        id="medicare", label="Medicare (Original)", category="medicare",
        level="plan",                 # enrollment in the CMS file IS plan-level confirmation
        method="cms-enrollment", source_url=source_url, states=None,
        bitmap=bitmap, fetched_at=fetched_at,
        max_age_days=settings.medicare_max_age_days,   # Medicare refreshes quarterly
    )
    print(f"medicare: admitted {admitted:,} NPIs, rejected {rejected:,} (failed NPI Luhn "
          f"or out of range). Bitmap cardinality {entry.count:,}.", flush=True)
    print(f"Wrote {root / entry.file} + manifest ({entry.sha256[:12]}…).", flush=True)
    return entry


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    cmd = argv[1]
    root = Path(settings.membership_dir)
    if cmd == "medicare":
        src = argv[2] if len(argv) > 2 else None
        build_medicare(src, root)
    else:
        print(f"Unknown command {cmd!r}. Known: medicare.")
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv)
