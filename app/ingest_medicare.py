"""Ingest the CMS Medicare Fee-For-Service Public Provider Enrollment file.

Every NPI in that file is approved to bill Medicare, so a hit is a truthful
"Accepts Medicare" signal. Re-run quarterly to refresh.

Usage:
    python -m app.ingest_medicare /path/to/enrollment.csv
    python -m app.ingest_medicare "https://data.cms.gov/.../enrollment.csv"
"""
import csv
import io
import sys
import time
from collections.abc import Iterable
from typing import TextIO

from . import db

# The public CMS dataset this index is built from — used as the provenance/verify
# link when the operator ingests from a local file rather than the live URL.
CMS_ENROLLMENT_URL = (
    "https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/"
    "medicare-fee-for-service-public-provider-enrollment"
)


def _open_source(src: str) -> TextIO:
    if src.startswith(("http://", "https://")):
        from .download import stream_to_spool
        print(f"Downloading {src} ...", flush=True)
        # Stream to a spooled temp file (bounded memory, aborts past the cap) and
        # parse it incrementally — the enrollment CSV is never held whole in RAM.
        spool = stream_to_spool(src)  # bounded by settings.ingest_max_bytes
        return io.TextIOWrapper(spool, encoding="utf-8-sig", newline="")
    # utf-8-sig strips the BOM CMS exports sometimes carry.
    return open(src, encoding="utf-8-sig", newline="")


def _find_npi_field(fieldnames: Iterable[str] | None) -> str:
    for f in fieldnames or []:
        if f and f.strip().upper().replace(" ", "_") in ("NPI", "NPI_NUMBER"):
            return f
    for f in fieldnames or []:  # looser fallback
        if f and "NPI" in f.upper():
            return f
    return ""


def ingest(src: str) -> int:
    db.init_db()
    fh = _open_source(src)
    try:
        reader = csv.DictReader(fh)
        npi_field = _find_npi_field(reader.fieldnames)
        if not npi_field:
            raise SystemExit(f"No NPI column found. Columns seen: {reader.fieldnames}")

        seen: set[str] = set()
        batch: list[str] = []
        added = 0
        for row in reader:
            npi = (row.get(npi_field) or "").strip()
            if len(npi) == 10 and npi.isdigit() and npi not in seen:
                seen.add(npi)
                batch.append(npi)
                if len(batch) >= 5000:
                    added += db.medicare_add_many(batch)
                    batch = []
        if batch:
            added += db.medicare_add_many(batch)
        # Record provenance: a verified Medicare answer is traceable to this source
        # with a fetch date. Use the live URL when ingesting from one, else the
        # canonical public dataset page.
        source_url = src if src.startswith(("http://", "https://")) else CMS_ENROLLMENT_URL
        db.source_meta_set("medicare", source_url, time.time())
        return added
    finally:
        fh.close()


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    added = ingest(argv[1])
    print(f"Ingested {added} unique Medicare-enrolled NPIs. "
          f"Index now holds {db.medicare_count()} total.")


if __name__ == "__main__":
    main(sys.argv)
