"""Rail 2 — streaming Transparency-in-Coverage (TiC) harvester.

Under the federal TiC rule (CMS-9915-F) every commercial plan publishes machine-readable
in-network files, public and login-free. This harvests the set of in-network NPIs for a
payer into a membership bitmap, so the payer becomes an instant local verified filter.

Two hard corrections over the old `app/ingest_tic.py` (which would silently harvest ~0
NPIs from most modern payers):

  1. STREAMING. TiC in-network files run to hundreds of GB. We never land the whole file:
     ijson event-parses the byte stream and plucks only `npi` arrays (and external
     `provider_references` locations), so memory stays flat regardless of file size.

  2. DEREFERENCE `provider_references`. Modern TiC files don't inline `provider_groups`
     under each rate — they factor provider groups into a top-level `provider_references`
     list, and many of those are EXTERNAL files referenced by `location`. The old parser
     only read inlined `provider_groups[].npi[]`, so it saw almost nothing. We collect NPIs
     from inlined groups AND follow every external `provider_references[].location`.

Trust: TiC membership is PAYER-network-level (`level="payer"`) — a hit means "listed in
the payer's in-network file", not "accepts your specific plan". Every NPI passes the Luhn
gate (app/npi.py), so a garbage identifier (a TIN in the NPI slot — the exact TiC failure
mode) can't fabricate a "yes". A table-of-contents root is auto-discovered and fanned out.

    python -m app.harvest_tic aetna https://.../aetna-index.json
    python -m app.harvest_tic cigna /path/to/in-network.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

import ijson
from pyroaring import BitMap

from . import membership, tic_index
from .catalog import PAYER_CATALOG
from .config import settings

_CATALOG_IDS = {e["id"] for e in PAYER_CATALOG}
_CATALOG = {e["id"]: e for e in PAYER_CATALOG}


@dataclass
class TicStats:
    files: int = 0                 # in-network + external ref files streamed
    external_refs: int = 0         # provider_references locations followed
    npi_values_seen: int = 0       # every npi token encountered (incl. dupes)
    rejected: int = 0              # failed the Luhn gate
    unique_npis: int = 0           # final bitmap cardinality
    failed_files: int = 0          # ToC children / external refs that couldn't be read
    failures: list[str] = field(default_factory=list)  # "<src>: <why>" per hole (+ probe cap)
    error: str | None = None       # a fatal error that aborted the whole harvest

    @property
    def complete(self) -> bool:
        """A harvest is complete only if nothing errored, no file failed, and no probe cap
        was hit — the write gate mirrors Rail 1: any hole means a partial fan-out that must
        NOT be served as complete (a missing in-network file would read as a fabricated no)."""
        return self.error is None and not self.failures


def _open_binary(src: str) -> tuple[IO[bytes], list[Any]]:
    """Open `src` (URL or path) as a seekable binary stream, transparently gunzipping.
    Returns (stream, closers). A URL streams to a disk-backed spool (bounded by
    settings.ingest_max_bytes) so even a huge file never lands in RAM."""
    closers: list[Any] = []
    if src.startswith(("http://", "https://")):
        from .download import stream_to_spool
        raw: IO[bytes] = stream_to_spool(src)  # rewound, seekable, disk-backed
    else:
        raw = open(src, "rb")
    closers.append(raw)
    head = raw.read(2)
    raw.seek(0)
    if head == b"\x1f\x8b" or src.endswith(".gz"):
        gz = gzip.GzipFile(fileobj=raw)
        closers.insert(0, gz)
        # GzipFile is a binary stream but typeshed doesn't type it as IO[bytes]; the cast
        # reflects the real contract (ijson reads it like any binary file object).
        return cast("IO[bytes]", gz), closers
    return raw, closers


def _top_level_keys(stream: IO[bytes], limit: int = 8) -> list[str]:
    """The first few top-level object keys, to classify a file (ToC vs in-network) without
    reading it whole. Rewinds the stream afterward (callers pass a seekable stream)."""
    keys: list[str] = []
    for prefix, event, value in ijson.parse(stream):
        if event == "map_key" and prefix == "":
            keys.append(value)
            if len(keys) >= limit:
                break
    stream.seek(0)
    return keys


def _stream_npis(stream: IO[bytes], external_refs: list[str]) -> Iterator[str]:
    """Single streaming pass: yield every NPI token (from inlined provider_groups anywhere
    in the doc) and record every external `provider_references[].location` for follow-up.

    The `.npi.item` suffix matches an npi under BOTH `in_network[].negotiated_rates[]
    .provider_groups[]` and top-level `provider_references[].provider_groups[]`, and the
    root `provider_groups[]` of an external ref file — one rule covers every shape.
    """
    for prefix, event, value in ijson.parse(stream):
        if prefix.endswith(".npi.item") and event in ("number", "string"):
            # npi is usually an int in TiC; normalize both int and string forms.
            yield str(value) if event == "string" else str(int(value))
        elif event == "string" and prefix.endswith("provider_references.item.location"):
            if value:
                external_refs.append(value)


class TicHarvester:
    """Accumulates in-network NPIs directly into a Roaring bitmap (bounded memory even for
    millions of NPIs), following TiC indexes and external provider references."""

    def __init__(self, max_files: int | None = None) -> None:
        self.bitmap = BitMap()
        self.stats = TicStats()
        self._visited: set[str] = set()
        self.max_files = max_files       # recon cap on in-network files streamed (probe run)
        self._capped = False

    def _admit(self, npi: str) -> None:
        self.stats.npi_values_seen += 1
        v = membership.encode(npi)
        if v is None:
            self.stats.rejected += 1
        else:
            self.bitmap.add(v)   # idempotent — the bitmap is the dedup

    def harvest(self, src: str) -> None:
        """Harvest `src`, which may be a TiC table-of-contents (fanned out), an in-network
        file, or an external provider-reference file. Cycle-guarded via `_visited`.

        Resilient by design: a child/external file that fails to open or stream is recorded
        as a HOLE (`stats.failures`) and does NOT abort its siblings — so one run surfaces
        the whole failure picture, and the completeness guard then refuses to ship the
        partial set (a missing in-network file would make its providers read as "no")."""
        if src in self._visited or self._capped:
            return
        self._visited.add(src)
        child_srcs: list[str] = []
        external_after: list[str] = []
        try:
            stream, closers = _open_binary(src)
            try:
                keys = _top_level_keys(stream)
                if "reporting_structure" in keys:
                    # A table-of-contents. It's the small index file (not an in-network file),
                    # so reading it whole to discover the in-network URLs is fine; fan out.
                    refs = tic_index.parse_index(stream.read())
                    child_srcs = [r.location for r in refs]
                else:
                    # An in-network file (or external ref file). Honor the recon cap BEFORE
                    # streaming another file so a `--max-files` probe stays cheap; hitting it
                    # marks the run incomplete (a capped run must never write as complete).
                    if self.max_files is not None and self.stats.files >= self.max_files:
                        self._capped = True
                        self.stats.failures.append(
                            f"hit --max-files cap ({self.max_files}) — probe run, not complete")
                        return
                    self.stats.files += 1
                    for npi in _stream_npis(stream, external_after):
                        self._admit(npi)
            finally:
                for c in closers:
                    try:
                        c.close()
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001 - one bad file is a hole, not a crash
            self.stats.failed_files += 1
            self.stats.failures.append(f"{src}: {type(e).__name__}: {e}")
            return
        # Fan out to ToC children, then to external provider-reference files.
        for child in child_srcs:
            self.harvest(child)
        for ext in external_after:
            self.stats.external_refs += 1
            self.harvest(_resolve_ref(src, ext))

    def finalize(self) -> TicStats:
        self.stats.unique_npis = len(self.bitmap)
        return self.stats


def _resolve_ref(parent: str, ref: str) -> str:
    """Resolve an external provider-reference location. Absolute URLs/paths pass through;
    a relative URL is resolved against its parent in-network file's URL."""
    if ref.startswith(("http://", "https://")) or Path(ref).is_absolute():
        return ref
    if parent.startswith(("http://", "https://")):
        from urllib.parse import urljoin
        return urljoin(parent, ref)
    return str(Path(parent).parent / ref)


def harvest_to_bitmap(payer_id: str, src: str, root: Path, *, complete_only: bool = True,
                      max_files: int | None = None) -> tuple[membership.ManifestEntry | None, TicStats]:
    """Harvest `payer_id` from TiC source `src` and write its membership bitmap (method
    "tic", level "payer"). Returns (entry, stats); entry is None (nothing written) when:

      • the harvest collected nothing (never overwrite a good bitmap with an empty one); or
      • `complete_only` and the fan-out was PARTIAL — a ToC child or external provider-ref
        file failed to read, a fatal error aborted the walk, or a `--max-files` probe cap was
        hit. This mirrors Rail 1's completeness guard: a partial in-network set served as
        complete would make providers in the missing files read as a fabricated "no", so a
        partial run keeps the last-good bitmap and lets `/healthz` staleness surface instead.
    """
    if payer_id not in _CATALOG_IDS:
        print(f"Warning: '{payer_id}' is not in app/catalog.py — it won't surface as a "
              f"named filter until added there.", flush=True)
    h = TicHarvester(max_files=max_files)
    try:
        h.harvest(src)
    except Exception as e:  # noqa: BLE001
        h.stats.error = f"{type(e).__name__}: {e}"
    stats = h.finalize()
    if stats.unique_npis == 0:
        return None, stats
    if complete_only and not stats.complete:
        return None, stats
    cat = _CATALOG.get(payer_id, {})
    entry = membership.write_payer(
        root,
        id=payer_id, label=cat.get("label", payer_id),
        category=cat.get("category", "commercial"),
        level="payer",                 # TiC = listed in the payer's in-network file
        method="tic", source_url=src,
        states=cat.get("states"), bitmap=h.bitmap,
        max_age_days=settings.payer_max_age_days,
    )
    return entry, stats


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(
        description="Streaming Transparency-in-Coverage harvest -> membership bitmap.")
    ap.add_argument("payer", help="catalog id (e.g. aetna, anthem) so the bitmap supersedes the estimate")
    ap.add_argument("src", help="TiC table-of-contents index URL/path, or a single in-network file")
    ap.add_argument("--max-files", type=int, default=None,
                    help="recon cap on in-network files streamed; hitting it marks the run "
                         "incomplete (a probe never writes a partial set as complete)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="opt out of the completeness guard and write whatever was collected "
                         "(unsafe — a hole reads as a fabricated 'no'; for debugging only)")
    args = ap.parse_args(argv[1:])

    root = Path(settings.membership_dir)
    entry, stats = harvest_to_bitmap(args.payer, args.src, root,
                                     complete_only=not args.allow_partial, max_files=args.max_files)
    payer = args.payer
    print(f"[{payer}] files={stats.files} external_refs={stats.external_refs} "
          f"npi_tokens={stats.npi_values_seen:,} rejected={stats.rejected:,} "
          f"unique={stats.unique_npis:,} failed_files={stats.failed_files}", flush=True)
    if stats.error:
        print(f"[{payer}] stopped early: {stats.error}", flush=True)
    for f in stats.failures[:10]:
        print(f"[{payer}]   hole: {f}", flush=True)
    if entry is not None:
        print(f"[{payer}] wrote {root / entry.file} ({entry.count:,} NPIs, "
              f"{entry.sha256[:12]}…).", flush=True)
    elif stats.unique_npis and not stats.complete:
        print(f"[{payer}] partial fan-out ({stats.unique_npis:,} NPIs but "
              f"{len(stats.failures)} hole(s)) — bitmap NOT written (kept last good; "
              f"staleness surfaces).", flush=True)
    else:
        print(f"[{payer}] harvest collected 0 NPIs — bitmap NOT written (kept last good).",
              flush=True)


if __name__ == "__main__":
    main(sys.argv)
