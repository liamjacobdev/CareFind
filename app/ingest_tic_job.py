"""Scheduled per-payer Transparency-in-Coverage ingestion (verified rail B).

Wraps app.ingest_tic in a job that pulls each configured payer's published TiC
in-network index, extracts NPIs, and ingests them — making that payer a verified
("Confirmed") filter nationally, exactly like Medicare. This is the automation layer
over the one-off `python -m app.ingest_tic <payer> <src>` command.

Sources are read from tic_sources.json (see tic_sources.example.json):
    { "sources": [ {"payer": "aetna", "url": "https://payer.example/in-network.json.gz"} ] }
Each `payer` must match a catalog id in app/catalog.py so the verified ingest
supersedes the estimate. Document each URL + retrieval date in the README.

Re-running is idempotent — NPIs are stored INSERT OR IGNORE — so this is safe on a
monthly cron:
    python -m app.ingest_tic_job              # refresh every configured payer
    python -m app.ingest_tic_job aetna        # refresh just one configured payer
    python -m app.ingest_tic_job aetna URL    # ad-hoc: ingest a payer from a URL/path
"""
import json
import sys
from pathlib import Path
from typing import Any

from . import db, ingest_tic
from .catalog import PAYER_CATALOG
from .config import settings
from .insurance import Registry

_CATALOG_IDS = {e["id"] for e in PAYER_CATALOG}


def load_sources(path: str | None = None) -> list[dict[str, Any]]:
    """Read tic_sources.json -> [{"payer","url",...}]. Missing file -> []."""
    p = Path(path or settings.tic_sources_file)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("sources", []) or []
    return data if isinstance(data, list) else []


def _is_verified(payer: str) -> bool:
    """True once the payer surfaces as a verified plan (its TiC ingest makes the
    TicSource available and supersede the estimate) — the acceptance signal."""
    reg = Registry()
    reg.build()
    return any(p["id"] == payer and p["confidence"] == "verified" for p in reg.plans())


def ingest_payer(payer: str, url: str) -> dict[str, Any]:
    """Ingest one payer from its source URL/path and report the outcome. Idempotent."""
    if payer not in _CATALOG_IDS:
        print(f"Warning: '{payer}' is not in app/catalog.py — it won't surface as a "
              f"named filter until you add it there.", flush=True)
    added = ingest_tic.ingest(payer, url)
    total = db.tic_count(payer)
    verified = _is_verified(payer)
    print(f"[{payer}] ingested {added} NPIs from {url} — index now holds {total}; "
          f"verified filter: {'YES' if verified else 'no'}", flush=True)
    return {"payer": payer, "added": added, "total": total, "verified": verified}


def run(only_payer: str | None = None, sources_path: str | None = None) -> list[dict[str, Any]]:
    """Ingest every configured payer (or just `only_payer`). Returns per-payer results."""
    db.init_db()
    sources = load_sources(sources_path)
    if only_payer:
        sources = [s for s in sources if s.get("payer") == only_payer]
        if not sources:
            raise SystemExit(
                f"No source for '{only_payer}' in {sources_path or settings.tic_sources_file}. "
                f"Add it there, or pass a URL: python -m app.ingest_tic_job {only_payer} <url>")
    if not sources:
        raise SystemExit(
            f"No TiC sources configured. Copy tic_sources.example.json to "
            f"{settings.tic_sources_file} and add each payer's in-network URL.")
    results: list[dict[str, Any]] = []
    for s in sources:
        payer, url = s.get("payer"), s.get("url")
        if not payer or not url:
            print(f"Skipping malformed source entry: {s!r}", flush=True)
            continue
        results.append(ingest_payer(payer, url))
    return results


def main(argv: list[str]) -> None:
    # Ad-hoc two-arg form: payer + explicit URL/path (no config needed).
    if len(argv) >= 3:
        db.init_db()
        ingest_payer(argv[1], argv[2])
        return
    only = argv[1] if len(argv) == 2 else None
    run(only_payer=only)


if __name__ == "__main__":
    main(sys.argv)
