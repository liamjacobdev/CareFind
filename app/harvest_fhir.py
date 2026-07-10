"""Rail 1 — offline FHIR Plan-Net bulk harvester.

The old model asked a payer's FHIR directory live, once per NPI, at search time. This
walks the payer's ENTIRE Plan-Net directory *offline* and collects the NPIs that are
truly in-network, into a Roaring membership bitmap (app/membership.py). At serve time
the payer is then an instant local set-membership test — no live calls, on by default.

The trust judgement is unchanged — it is moved, not weakened. An NPI is admitted only if
the directory carries an **active PractitionerRole with a resolvable network link** for
it (`FhirPlanNetSource._has_network_link` — presence alone never qualifies), which is
exactly `_in_network(...) is True` applied per role instead of per NPI. Every admitted
NPI additionally passes the Luhn check-digit gate (app/npi.py), so a malformed identifier
in the directory can't fabricate a "yes".

Mechanics: paginate `PractitionerRole?_include=PractitionerRole:practitioner` (so each
page carries both the roles and the Practitioner resources they reference), follow the
Bundle `next` link, and for every active + network-linked role resolve its practitioner
to an NPI. Transient failures back off and retry; the `next` URL is a resumable cursor,
and `browse_params` accepts a shard facet so the giants (Humana/UHC) can be split across
a CI job matrix. A harvest that yields far fewer NPIs than roles seen is surfaced loudly.

    python -m app.harvest_fhir cigna --max-pages 50
    python -m app.harvest_fhir humana --shard state=CA        # a matrix shard
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import membership, planet_registry
from .config import settings
from .fhir_source import FhirPlanNetSource
from .npi import luhn_valid
from .oauth import ClientCredentials

_NPI_SYSTEMS = ("http://hl7.org/fhir/sid/us-npi",)


@dataclass
class HarvestStats:
    pages: int = 0
    roles_seen: int = 0
    roles_in_network: int = 0          # active + network-linked
    npis_admitted: int = 0             # unique, Luhn-valid, in-network
    practitioner_unresolved: int = 0   # in-network role whose Practitioner wasn't on the page
    npi_rejected: int = 0              # in-network NPI that failed the Luhn gate
    retries: int = 0
    next_cursor: str | None = None     # the unfetched `next` URL, for resumption
    error: str | None = None


def _practitioner_npi(res: dict[str, Any]) -> str | None:
    """The Luhn-valid NPI on a Practitioner resource, or None. The Luhn gate here is what
    stops a garbage identifier (a TIN in the NPI slot) from entering a verified set."""
    for idt in res.get("identifier", []) or []:
        system = idt.get("system", "") or ""
        value = (idt.get("value") or "").strip()
        if ("us-npi" in system or system in _NPI_SYSTEMS) and luhn_valid(value):
            return value
    return None


def _practitioner_index(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Map every included Practitioner to its NPI, keyed by both bare id and
    `Practitioner/{id}` (and fullUrl) so a role's `practitioner.reference` resolves however
    the server phrased it."""
    idx: dict[str, str] = {}
    for e in entries:
        res = e.get("resource") or {}
        if res.get("resourceType") != "Practitioner":
            continue
        npi = _practitioner_npi(res)
        if not npi:
            continue
        rid = res.get("id")
        if rid:
            idx[rid] = npi
            idx[f"Practitioner/{rid}"] = npi
        full = e.get("fullUrl")
        if full:
            idx[full] = npi
    return idx


def harvest_page(bundle: dict[str, Any], out: set[str], stats: HarvestStats) -> None:
    """Collect in-network NPIs from one page into `out`, updating `stats`.

    A role contributes its practitioner's NPI iff it is the exact `_in_network` True case:
    an active PractitionerRole carrying a resolvable network link.
    """
    entries = bundle.get("entry", []) if isinstance(bundle, dict) else []
    prac = _practitioner_index(entries)
    for e in entries:
        res = e.get("resource") or {}
        if res.get("resourceType") != "PractitionerRole":
            continue
        stats.roles_seen += 1
        if res.get("active") is False:
            continue
        if not FhirPlanNetSource._has_network_link(res):
            continue
        stats.roles_in_network += 1
        ref = (res.get("practitioner") or {}).get("reference") or ""
        npi = prac.get(ref) or prac.get(ref.split("/")[-1] if ref else "")
        if not npi:
            stats.practitioner_unresolved += 1
            continue
        # _practitioner_index already Luhn-gated; this stays defensive + counts rejects.
        if not luhn_valid(npi):
            stats.npi_rejected += 1
            continue
        out.add(npi)


def _headers(cfg: dict[str, Any], client: httpx.Client) -> dict[str, str]:
    h = {"Accept": "application/fhir+json", "User-Agent": settings.contact_ua}
    if cfg.get("api_key_header") and cfg.get("api_key"):
        h[cfg["api_key_header"]] = cfg["api_key"]
    cc = ClientCredentials.from_config(cfg)
    if cc is not None:
        tok = cc.token_sync(client)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    return h


def _next_url(bundle: dict[str, Any]) -> str | None:
    for link in bundle.get("link", []) or []:
        if link.get("relation") == "next" and link.get("url"):
            return str(link["url"])
    return None


def _page_fatal_outcome(bundle: dict[str, Any], roles_on_page: int) -> str | None:
    """If a page carries NO matched roles but an error/fatal OperationOutcome, return its
    diagnostic. FHIR servers wrap errors (e.g. a too-large `_count`, a rejected facet) in a
    searchset Bundle with a 200 status, so without this a broken browse looks like a
    legitimately empty directory and would ship an empty "verified" set. A warning attached
    to a page that DID return roles is ignored (not fatal)."""
    if roles_on_page:
        return None
    for e in bundle.get("entry", []) or []:
        res = e.get("resource") or {}
        if res.get("resourceType") != "OperationOutcome":
            continue
        for issue in res.get("issue", []) or []:
            if issue.get("severity") in ("error", "fatal"):
                code = issue.get("code", "")
                diag = issue.get("diagnostics") or issue.get("details", {}).get("text", "")
                return f"OperationOutcome {code}: {diag}".strip()
    return None


def _get_with_backoff(client: httpx.Client, url: str, headers: dict[str, str],
                      params: dict[str, Any] | None, stats: HarvestStats,
                      *, attempts: int = 4, timeout: float = 30.0) -> httpx.Response:
    """GET with bounded exponential backoff on transient failures (429/5xx/network). A
    persistent failure raises — the caller stops and records the cursor for resumption, so
    a harvest is never silently short (which would ship an incomplete "verified" set)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = client.get(url, headers=headers, params=params, timeout=timeout,
                           follow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("transient", request=r.request, response=r)
            return r
        except Exception as e:  # noqa: BLE001 - retry any transient error
            last = e
            if i < attempts - 1:
                stats.retries += 1
                time.sleep(min(2 ** i, 10))
    assert last is not None
    raise last


def harvest_endpoint(
    cfg: dict[str, Any],
    *,
    page_size: int = 100,
    max_pages: int | None = None,
    max_npis: int | None = None,
    browse_params: dict[str, Any] | None = None,
    start_url: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[set[str], HarvestStats]:
    """Walk `cfg`'s Plan-Net directory and return (in-network NPIs, stats).

    `browse_params` overrides the default browse (`_include` + `_count`); add a facet here
    to shard a large directory. `start_url` resumes from a previously recorded `next`
    cursor. `max_pages`/`max_npis` bound a partial/sampling run.
    """
    base = cfg["base_url"].rstrip("/")
    owns_client = client is None
    client = client or httpx.Client()
    out: set[str] = set()
    stats = HarvestStats()
    params = browse_params or {"_include": "PractitionerRole:practitioner", "_count": page_size}
    url: str | None = start_url or f"{base}/PractitionerRole"
    # Params ride only on the first request; the server bakes them into the `next` URL.
    next_params: dict[str, Any] | None = None if start_url else params
    try:
        headers = _headers(cfg, client)
        while url:
            if max_pages is not None and stats.pages >= max_pages:
                stats.next_cursor = url
                break
            if max_npis is not None and len(out) >= max_npis:
                stats.next_cursor = url
                break
            r = _get_with_backoff(client, url, headers, next_params, stats)
            if r.status_code == 404:
                break
            r.raise_for_status()
            bundle = r.json()
            if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
                stats.error = "browse did not return a FHIR Bundle (endpoint needs a facet/shard)"
                break
            roles_before = stats.roles_seen
            harvest_page(bundle, out, stats)
            fatal = _page_fatal_outcome(bundle, stats.roles_seen - roles_before)
            if fatal is not None:
                # A too-large _count / rejected facet / server error, wrapped in a 200
                # Bundle. Stop loudly and keep the cursor rather than shipping a short set.
                stats.error = fatal
                stats.next_cursor = url
                break
            stats.pages += 1
            url = _next_url(bundle)
            next_params = None
    except Exception as e:  # noqa: BLE001
        stats.error = f"{type(e).__name__}: {e}"
        stats.next_cursor = url
    finally:
        if owns_client:
            client.close()
    stats.npis_admitted = len(out)
    return out, stats


def _resolve_cfg(payer_id: str) -> dict[str, Any]:
    """The payers.json-shaped config for `payer_id`, from operator payers.json first, then
    the validated public registry (so a harvest uses the same trust-gated source list)."""
    for c in settings.load_payers():
        if (c or {}).get("id") == payer_id:
            return c
    for e in planet_registry.REGISTRY:
        if e.id == payer_id:
            return e.payer_config()
    raise SystemExit(f"Unknown payer {payer_id!r}. Known validated: "
                     f"{[e.id for e in planet_registry.validated()]}")


def harvest_to_bitmap(payer_id: str, root: Path, *, complete_only: bool = True,
                      **kw: Any) -> tuple[membership.ManifestEntry | None, HarvestStats]:
    """Harvest `payer_id` and write its membership bitmap + manifest entry (method
    "fhir-plannet"). Returns (entry, stats); entry is None (nothing written) when:

      • the harvest collected nothing (don't overwrite a good bitmap with an empty one); or
      • `complete_only` and the harvest did NOT exhaust the directory (a `next_cursor` was
        left, or it errored). This is the trust guard: a PARTIAL set served as if complete
        would make in-network providers beyond the harvested pages read as False — a
        fabricated "no". A partial/sampling run (`--max-pages`, a timeout, an upstream
        error) therefore keeps the last-good bitmap and lets staleness surface instead.
    """
    cfg = _resolve_cfg(payer_id)
    npis, stats = harvest_endpoint(cfg, **kw)
    if not npis:
        return None, stats
    if complete_only and (stats.next_cursor is not None or stats.error is not None):
        return None, stats
    bitmap, admitted, rejected = membership.build_bitmap(npis)
    entry = membership.write_payer(
        root,
        id=cfg["id"], label=cfg.get("label", cfg["id"]),
        category=cfg.get("category", "commercial"),
        level="payer",                         # FHIR network membership is payer-level
        method="fhir-plannet",
        source_url=cfg.get("verify_url") or cfg.get("directory_url") or cfg["base_url"],
        states=cfg.get("states"), bitmap=bitmap,
        max_age_days=settings.payer_max_age_days,
    )
    stats.npi_rejected += rejected
    return entry, stats


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="Offline FHIR Plan-Net bulk harvest -> membership bitmap.")
    ap.add_argument("payer", help="validated payer id (e.g. cigna, unitedhealthcare, humana)")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--max-npis", type=int, default=None)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--shard", default=None,
                    help="a browse facet appended to the query, e.g. 'state=CA' (endpoint-specific)")
    ap.add_argument("--dry-run", action="store_true", help="harvest + report, don't write the bitmap")
    args = ap.parse_args(argv[1:])

    browse: dict[str, Any] = {"_include": "PractitionerRole:practitioner", "_count": args.page_size}
    if args.shard and "=" in args.shard:
        k, v = args.shard.split("=", 1)
        browse[k] = v
    root = Path(settings.membership_dir)

    if args.dry_run:
        cfg = _resolve_cfg(args.payer)
        npis, stats = harvest_endpoint(cfg, page_size=args.page_size, max_pages=args.max_pages,
                                       max_npis=args.max_npis, browse_params=browse)
        entry = None
    else:
        entry, stats = harvest_to_bitmap(args.payer, root, page_size=args.page_size,
                                         max_pages=args.max_pages, max_npis=args.max_npis,
                                         browse_params=browse)
    print(f"[{args.payer}] pages={stats.pages} roles_seen={stats.roles_seen:,} "
          f"in_network={stats.roles_in_network:,} npis={stats.npis_admitted:,} "
          f"unresolved={stats.practitioner_unresolved:,} rejected={stats.npi_rejected:,} "
          f"retries={stats.retries}", flush=True)
    if stats.error:
        print(f"[{args.payer}] stopped early: {stats.error}", flush=True)
    if stats.next_cursor:
        print(f"[{args.payer}] resume cursor: {stats.next_cursor}", flush=True)
    if entry is not None:
        print(f"[{args.payer}] wrote {root / entry.file} ({entry.count:,} NPIs, "
              f"{entry.sha256[:12]}…).", flush=True)
    elif not args.dry_run:
        why = ("incomplete harvest (a resume cursor or error was left — a partial set is "
               "never served as complete)" if stats.npis_admitted else "collected 0 NPIs")
        print(f"[{args.payer}] {why} — bitmap NOT written (kept last good; staleness surfaces).",
              flush=True)


if __name__ == "__main__":
    main(sys.argv)
