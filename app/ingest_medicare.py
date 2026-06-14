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

from . import db


def _open_source(src: str):
    if src.startswith(("http://", "https://")):
        import httpx
        print(f"Downloading {src} ...", flush=True)
        resp = httpx.get(src, follow_redirects=True, timeout=300)
        resp.raise_for_status()
        return io.StringIO(resp.text)
    # utf-8-sig strips the BOM CMS exports sometimes carry.
    return open(src, "r", encoding="utf-8-sig", newline="")


def _find_npi_field(fieldnames) -> str:
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

        seen: set = set()
        batch: list = []
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
        return added
    finally:
        fh.close()


def main(argv) -> None:
    if len(argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    added = ingest(argv[1])
    print(f"Ingested {added} unique Medicare-enrolled NPIs. "
          f"Index now holds {db.medicare_count()} total.")


if __name__ == "__main__":
    main(sys.argv)
