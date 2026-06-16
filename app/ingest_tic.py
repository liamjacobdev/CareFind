"""Ingest a payer's Transparency-in-Coverage (TiC) in-network NPIs.

Under the federal Transparency in Coverage rule, every commercial health plan must
publish machine-readable in-network files. CareFind ingests the set of in-network
NPIs for a given payer so that payer becomes a VERIFIED filter (a green "Confirmed"
badge), exactly like Medicare. The payer id must match a catalog entry in
app/catalog.py (e.g. "aetna", "cigna", "unitedhealthcare") so the verified ingest
supersedes the estimated catalog entry.

TiC in-network files are large and vary by payer. Two input shapes are supported:

  1. A simple newline- or CSV-style list of 10-digit NPIs (one per line, or a CSV
     with an NPI column) — handy after you've pre-extracted NPIs from a payer file.
  2. A TiC in-network JSON file (or .json.gz): we stream it and collect every
     `provider_groups[].npi[]` value under `in_network[]`.

Usage:
    python -m app.ingest_tic aetna /path/to/aetna_npis.csv
    python -m app.ingest_tic cigna "https://payer.example/in-network.json.gz"

Re-run when the payer republishes (monthly). A hit is a truthful in-network signal.
"""
import csv
import gzip
import io
import json
import sys
import time
from collections.abc import Iterator
from typing import Any

from . import db, tic_index
from .catalog import PAYER_CATALOG

_CATALOG_IDS = {e["id"] for e in PAYER_CATALOG}


def _open_bytes(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        from .download import stream_to_bytes
        print(f"Downloading {src} ...", flush=True)
        return stream_to_bytes(src)  # bounded by settings.ingest_max_bytes
    with open(src, "rb") as fh:
        return fh.read()


def _maybe_gunzip(raw: bytes, src: str) -> bytes:
    if src.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _npis_from_text(raw: bytes) -> Iterator[str]:
    """List/CSV of NPIs: yield any 10-digit numeric token found in an NPI-ish column."""
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    if "," in sample and "\n" in sample:
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        idx = next((i for i, h in enumerate(header) if "NPI" in (h or "").upper()), 0)
        for row in reader:
            if idx < len(row):
                yield row[idx]
    else:
        yield from text.splitlines()


def _npis_from_tic_json(raw: bytes) -> Iterator[Any]:
    """Collect provider_groups[].npi[] under in_network[] of a TiC file."""
    data = json.loads(raw)
    for item in (data.get("in_network", []) if isinstance(data, dict) else []):
        for grp in item.get("provider_groups", []) or []:
            yield from grp.get("npi", []) or []


def _npis_from_raw(raw: bytes) -> Iterator[Any]:
    stripped = raw.lstrip()
    producer = _npis_from_tic_json if stripped[:1] in (b"{", b"[") else _npis_from_text
    yield from producer(raw)


def _ingest_src(payer: str, src: str, seen: set[str], visited: set[str]) -> int:
    """Ingest one source. If it's a TiC table-of-contents (C2), fan out across the
    discovered in-network files; otherwise parse NPIs directly. `seen` dedups NPIs
    across every file; `visited` guards against an index referencing itself in a cycle."""
    if src in visited:
        return 0
    visited.add(src)
    raw = _maybe_gunzip(_open_bytes(src), src)
    refs = tic_index.parse_index(raw)
    if refs:
        # A table-of-contents: ingest each discovered in-network file (recursively, so a
        # nested index also resolves). Surfacing the count of files keeps a large fan-out
        # observable.
        print(f"[{payer}] index: discovered {len(refs)} in-network file(s) from {src}", flush=True)
        return sum(_ingest_src(payer, r.location, seen, visited) for r in refs)

    added = 0
    batch: list[str] = []
    for npi in _npis_from_raw(raw):
        npi = str(npi or "").strip()
        if len(npi) == 10 and npi.isdigit() and npi not in seen:
            seen.add(npi)
            batch.append(npi)
            if len(batch) >= 5000:
                added += db.tic_add_many(payer, batch)
                batch = []
    if batch:
        added += db.tic_add_many(payer, batch)
    return added


def ingest(payer: str, src: str) -> int:
    """Ingest a payer's in-network NPIs from `src`, which may be a single in-network
    file OR a TiC table-of-contents index (auto-discovered and fanned out, C2)."""
    if payer not in _CATALOG_IDS:
        print(f"Warning: '{payer}' is not in app/catalog.py — it won't surface as a "
              f"named filter until you add it there.", flush=True)
    db.init_db()
    added = _ingest_src(payer, src, set(), set())
    # Provenance: a verified in-network answer for this payer traces back to the TiC
    # source it was ingested from, with a fetch date (the A3 trust rule). For an index,
    # that's the published root URL the operator configured.
    db.source_meta_set(payer, src, time.time())
    return added


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    payer, src = argv[1], argv[2]
    added = ingest(payer, src)
    print(f"Ingested {added} in-network NPIs for '{payer}'. "
          f"Index now holds {db.tic_count(payer)} for this payer.")


if __name__ == "__main__":
    main(sys.argv)
