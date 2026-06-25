"""Live-validate the public FHIR Plan-Net registry and regenerate the provenance ledger.

Validation is strict and uses CareFind's *own* in-network determination, so an endpoint
is only "validated" if it can answer the exact per-NPI lookup the app performs — without
lying in either direction:

  1. Reachable, and `{base}/PractitionerRole` returns a FHIR Bundle.
  2. A **bogus** NPI must NOT resolve to in-network (`_in_network is not True`) — otherwise
     the directory ignores the NPI filter and would mark everyone in-network (fabricated
     "yes"; e.g. CT's Medicaid directory).
  3. A **real, listed** NPI (discovered from the directory's own /Practitioner page) must
     resolve to in-network (`_in_network is True`) under the active strictness — otherwise
     per-NPI search returns nothing for everyone (fabricated "no"; e.g. Premera, several
     state Medicaid directories).

Only endpoints passing all three are wired as verified. The validator writes
docs/provenance.md — the human-readable ledger behind the README table and every
"Verify · checked <date>" link.

    python -m app.verify_payers            # live-check every endpoint, rewrite the ledger
    python -m app.verify_payers --offline  # rewrite the ledger from recorded status only
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path
from typing import Any

import httpx

from . import planet_registry
from .config import settings
from .insurance import FhirPlanNetSource
from .oauth import ClientCredentials
from .planet_registry import PlanNetEndpoint

_LEDGER_PATH = Path(__file__).resolve().parent.parent / "docs" / "provenance.md"
_TIMEOUT = 20.0
_BOGUS_NPI = "0000000000"


def _headers() -> dict[str, str]:
    return {"Accept": "application/fhir+json", "User-Agent": settings.contact_ua}


def _endpoint_headers(client: httpx.Client, e: PlanNetEndpoint) -> dict[str, str]:
    """Base headers plus a Bearer token for an OAuth-gated endpoint (e.g. Aetna). For
    open/static-key endpoints this is just the base headers."""
    headers = _headers()
    cc = ClientCredentials.from_config(e.payer_config())
    if cc is not None:
        tok = cc.token_sync(client)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    return headers


def _roles(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [e.get("resource", {}) for e in (bundle.get("entry") or [])
            if (e.get("resource") or {}).get("resourceType") == "PractitionerRole"]


def _query(client: httpx.Client, base: str, resource: str, headers: dict[str, str],
           **params: str) -> dict[str, Any]:
    r = client.get(f"{base}/{resource}", params=params, headers=headers,
                   timeout=_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def _discover_npi(client: httpx.Client, base: str, npi_system: str,
                  headers: dict[str, str]) -> str | None:
    """Pull a real, listed NPI from the directory's own /Practitioner page, to round-trip."""
    data = _query(client, base, "Practitioner", headers, _count="10")
    for entry in data.get("entry") or []:
        for idt in (entry.get("resource") or {}).get("identifier") or []:
            system = idt.get("system", "") or ""
            value = (idt.get("value") or "").strip()
            if ("us-npi" in system or system == npi_system) and len(value) == 10 and value.isdigit():
                return value
    return None


def _roles_for_npi(client: httpx.Client, base: str, system: str, npi: str,
                   lookup_mode: str, headers: dict[str, str]) -> dict[str, Any]:
    """The PractitionerRole bundle for one NPI, honoring the endpoint's lookup mode.
    "two_step" resolves the Practitioner by NPI then fetches its roles by reference — for
    directories (e.g. UnitedHealthcare/Optum) that don't support the chained search."""
    if lookup_mode == "two_step":
        p = _query(client, base, "Practitioner", headers, identifier=f"{system}|{npi}", _count="5")
        prac_ids = [res["id"] for ent in (p.get("entry") or [])
                    if (res := ent.get("resource") or {}).get("resourceType") == "Practitioner"
                    and res.get("id")]
        entries: list[dict[str, Any]] = []
        for pid in prac_ids[:3]:
            rb = _query(client, base, "PractitionerRole", headers,
                        practitioner=f"Practitioner/{pid}", _count="10")
            entries.extend(rb.get("entry") or [])
        return {"entry": entries}
    return _query(client, base, "PractitionerRole", headers,
                  **{"practitioner.identifier": f"{system}|{npi}", "_count": "5"})


def probe(client: httpx.Client, e: PlanNetEndpoint) -> dict[str, Any]:
    """Run the full per-NPI round-trip. Returns {status, total, error}."""
    base = e.base_url.rstrip("/")
    try:
        headers = _endpoint_headers(client, e)
        head = client.get(f"{base}/PractitionerRole", params={"_count": "1"}, headers=headers,
                          timeout=_TIMEOUT, follow_redirects=True)
        if head.status_code in (401, 403):
            return {"status": "gated", "total": None, "error": f"HTTP {head.status_code}"}
        head.raise_for_status()
        hd = head.json()
        if not (isinstance(hd, dict) and hd.get("resourceType") == "Bundle"):
            return {"status": "unusable", "total": None, "error": "response was not a FHIR Bundle"}
        total = hd.get("total")
        total = int(total) if isinstance(total, int) else None

        # 2) bogus NPI must NOT be judged in-network (else the filter is ignored).
        bogus = _roles_for_npi(client, base, e.npi_system, _BOGUS_NPI, e.lookup_mode, headers)
        if FhirPlanNetSource._in_network(bogus) is True:
            return {"status": "unusable", "total": total,
                    "error": "ignores the NPI filter (a bogus NPI resolves in-network)"}

        # 3) a real listed NPI must be judged in-network under the active strictness.
        npi = _discover_npi(client, base, e.npi_system, headers)
        if not npi:
            return {"status": "unusable", "total": total,
                    "error": "could not discover a listed NPI to round-trip"}
        real = _roles_for_npi(client, base, e.npi_system, npi, e.lookup_mode, headers)
        verdict = FhirPlanNetSource._in_network(real)
        if verdict is not True:
            return {"status": "unusable", "total": total,
                    "error": f"a listed NPI resolves to {verdict!r}, not in-network "
                             f"(no network link, or per-NPI search returns nothing)"}
        return {"status": "validated", "total": total, "error": None}
    except httpx.HTTPStatusError as exc:
        return {"status": "unusable", "total": None, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"status": "unreachable", "total": None, "error": f"{exc.__class__.__name__}: {exc}"}


def validate(offline: bool = False) -> list[dict[str, Any]]:
    """Validate every registry entry. Offline mode reports the recorded status without
    touching the network. Returns a result row per endpoint."""
    today = datetime.date.today().isoformat()
    results: list[dict[str, Any]] = []
    client = None if offline else httpx.Client()
    try:
        for e in planet_registry.REGISTRY:
            if offline or client is None:
                row = {"status": e.status, "total": e.bundle_total,
                       "checked": e.last_checked, "error": None}
            else:
                p = probe(client, e)
                row = {"status": p["status"], "total": p["total"],
                       "checked": today if p["status"] == "validated" else e.last_checked,
                       "error": p["error"]}
            results.append({"endpoint": e, **row})
    finally:
        if client is not None:
            client.close()
    return results


def render_ledger(results: list[dict[str, Any]]) -> str:
    """Render the provenance ledger as Markdown — the auto-current source-of-truth table."""
    generated = datetime.date.today().isoformat()
    lines = [
        "# Provenance ledger — public FHIR Plan-Net endpoints",
        "",
        "Auto-generated by `python -m app.verify_payers`. A **validated** endpoint passed the",
        "full per-NPI round-trip (bogus NPI -> not in-network; a real listed NPI -> in-network",
        "under CareFind's own determination) and is wired as a VERIFIED (\"Confirmed\") filter",
        "out of the box. Others are tracked, never wired: **gated** = needs developer",
        "registration; **unusable** = returns a Bundle but can't answer per-NPI truthfully",
        "(ignores the filter, or returns nothing for listed providers); **unreachable** = no",
        "response from the validation environment; **candidate** = not yet checked.",
        "",
        f"_Last generated: {generated}_",
        "",
        "| Payer / program | Catalog id | Category | States | Status | Bundle total | Checked | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rank = {"validated": 0, "gated": 1, "unusable": 2, "unreachable": 3, "candidate": 4}
    for row in sorted(results, key=lambda r: (rank.get(r["status"], 9), r["endpoint"].id)):
        e: PlanNetEndpoint = row["endpoint"]
        states = "national" if e.states is None else ", ".join(e.states)
        total = f"{row['total']:,}" if isinstance(row["total"], int) else "—"
        checked = row["checked"] or "—"
        note = (row.get("error") or e.note or "").replace("|", "\\|")
        lines.append(
            f"| {e.label} | `{e.id}` | {e.category} | {states} | "
            f"{row['status']} | {total} | {checked} | {note} |"
        )
    n_valid = sum(1 for r in results if r["status"] == "validated")
    lines += [
        "",
        f"**{n_valid} validated / {len(results)} tracked.** The freely-validatable, "
        "NPI-usable public set is small — most nationals gate behind developer registration "
        "and many public directories don't honor per-NPI search. This ledger grows only as "
        "endpoints genuinely pass the round-trip, never by assertion.",
        "",
    ]
    return "\n".join(lines)


def write_ledger(content: str, path: Path | None = None) -> Path:
    path = path or _LEDGER_PATH   # resolved at call time so tests can redirect it
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="Validate public Plan-Net endpoints + write the ledger.")
    ap.add_argument("--offline", action="store_true",
                    help="regenerate the ledger from recorded status without network calls")
    args = ap.parse_args(argv[1:])
    results = validate(offline=args.offline)
    path = write_ledger(render_ledger(results))
    for row in results:
        e = row["endpoint"]
        total = f"total {row['total']:,}" if isinstance(row["total"], int) else "—"
        err = f"  ({row['error']})" if row.get("error") else ""
        print(f"[{row['status']:>11}] {e.id:<20} {total}{err}", flush=True)
    n_valid = sum(1 for r in results if r["status"] == "validated")
    print(f"\n{n_valid}/{len(results)} validated. Ledger written to {path}.", flush=True)


if __name__ == "__main__":
    main(sys.argv)
