"""Curated registry of public FHIR Plan-Net Provider Directory endpoints (CMS-9115-F).

Under the CMS Interoperability rule, Medicare Advantage / Medicaid / CHIP / QHP issuers
must publish an *unauthenticated* FHIR R4 Plan-Net Provider Directory (Da Vinci PDEX
Plan-Net). This module curates the public, correctly **state-scoped** base URLs and
records each one's validation status. `app/verify_payers.py` live-checks them and
regenerates the provenance ledger (docs/provenance.md).

A `validated` entry is wired automatically as a VERIFIED ("Confirmed") filter:
`validated_payer_configs()` emits it in the same shape payers.json uses, and
`Registry.build()` loads it (gated by `settings.use_planet_registry`, default on) so a
fresh clone gets verified coverage with zero config.

What "validated" means here is strict, and was learned the hard way (see the notes
below): it is NOT enough that `{base}/PractitionerRole` returns a Bundle. The endpoint
must support the **per-NPI lookup CareFind actually performs** without lying in either
direction:
  • a bogus NPI must return an EMPTY result (else it ignores the filter and would mark
    *every* provider in-network — a fabricated "yes"; CT's directory does exactly this);
  • a real, listed NPI must return its PractitionerRole (else it returns nothing for
    everyone — a fabricated "no"; Premera and several state Medicaid directories do this).
Only an endpoint that passes both — verified by `app/verify_payers.py` — is wired.

Trust rules, non-negotiable:
  • NEVER map a regional licensee to a national catalog id (Premera is a WA/AK BCBS
    licensee, not national "bcbs").
  • `states` scopes the verified check: a regional endpoint is only queried for providers
    in those states (others are "unknown", never fabricated), which also avoids hammering
    a regional directory with out-of-state NPIs.
  • Honest ceiling: the freely-validatable, *NPI-usable* public set is small — most
    nationals gate behind developer registration and many public directories don't honor
    per-NPI search. The machinery here makes growing it turnkey; it never invents an
    endpoint or a result, and never wires one that can't answer per-NPI truthfully.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import settings

_NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"


@dataclass(frozen=True)
class PlanNetEndpoint:
    id: str                       # stable catalog id (also the plan id in the UI)
    label: str
    base_url: str
    category: str                 # medicaid | medicare_advantage | marketplace | commercial
    states: list[str] | None      # None -> national; a list -> regional (scopes the check)
    status: str = "candidate"     # "validated" | "gated" | "unusable" | "unreachable" | "candidate"
    bundle_total: int | None = None   # PractitionerRole Bundle.total at last check
    last_checked: str | None = None   # ISO date of the last successful validation
    npi_system: str = _NPI_SYSTEM
    note: str = ""

    def payer_config(self) -> dict[str, Any]:
        """The payers.json-shaped config FhirPlanNetSource consumes."""
        return {
            "id": self.id,
            "label": self.label,
            "payer": self.id,
            "category": self.category,
            "base_url": self.base_url,
            "npi_system": self.npi_system,
            "states": self.states,
            "verify_url": self.base_url,
        }


# ── The curated registry ──────────────────────────────────────────────────────
# `validated` entries each passed the full per-NPI round-trip (app/verify_payers.py):
# bogus NPI -> empty, and a real listed NPI -> an active PractitionerRole with a network
# link. Dates + totals are recorded and refreshed by the validator.
REGISTRY: list[PlanNetEndpoint] = [
    # ── Validated: public, unauthenticated, NPI-usable, network-linked ──
    PlanNetEndpoint(
        id="priority_partners", label="Priority Partners (JHHP, MD Medicaid)",
        base_url="https://api.jhhpfhir.com/r4/public-pp", category="medicaid",
        states=["MD"], status="validated", bundle_total=83024, last_checked="2026-06-16",
        note="Maryland Medicaid MCO (Johns Hopkins Health Plans). NPI round-trip verified.",
    ),
    PlanNetEndpoint(
        id="advantage_md", label="Johns Hopkins Advantage MD (Medicare Advantage)",
        base_url="https://api.jhhpfhir.com/r4/public-ma", category="medicare_advantage",
        states=["MD"], status="validated", bundle_total=107487, last_checked="2026-06-16",
        note="Johns Hopkins MA plan; same public host as Priority Partners. NPI round-trip verified.",
    ),
    # First validated NATIONAL commercial payer. Graduates the catalog's `cigna` estimate
    # to Confirmed (shared id = stable join key), so Cigna flips estimated->verified
    # everywhere with zero UI change.
    PlanNetEndpoint(
        id="cigna", label="Cigna",
        base_url="https://p-hi2.digitaledge.cigna.com/ProviderDirectory/v1", category="commercial",
        states=None, status="validated", last_checked="2026-06-23",
        note="National commercial. Public, unauthenticated Da Vinci PDEX Plan-Net; "
             "PractitionerRoles carry network-reference extensions to Cigna Network "
             "Organizations. NPI round-trip verified (bogus NPI -> empty; a listed NPI -> "
             "active, network-linked role).",
    ),

    # ── Tracked but NOT wired — each fails the per-NPI usability bar for a documented
    # reason. Re-check with app/verify_payers.py; if a payer fixes its directory it
    # graduates automatically. NEVER wire these as verified until they pass.
    PlanNetEndpoint(
        id="premera_bcbs", label="Premera Blue Cross (WA/AK)",
        base_url="https://opala.tech/provdir/premera/v1/fhir-r4", category="commercial",
        states=["WA", "AK"], status="unusable",
        note="Returns a Bundle, but PractitionerRole has no network links AND a real "
             "listed NPI returns 0 roles — so per-NPI lookup yields only unknown/false. "
             "Stays an ESTIMATED catalog filter, never verified, until the directory "
             "supports network-linked per-NPI search.",
    ),
    PlanNetEndpoint(
        id="ct_medicaid", label="Connecticut Medicaid (HUSKY) directory",
        base_url="https://ct-dss-medicaid.convergent-pd.com/fhir", category="medicaid",
        states=["CT"], status="unusable",
        note="UNSAFE: ignores practitioner.identifier — a bogus NPI returns thousands of "
             "providers, so wiring it would mark everyone in-network (fabricated yes).",
    ),
    PlanNetEndpoint(
        id="jhhp_ehp", label="Johns Hopkins EHP (commercial)",
        base_url="https://api.jhhpfhir.com/r4/public-ehp", category="commercial",
        states=["MD"], status="gated",
        note="Same host pattern as Priority Partners/Advantage MD, but returned HTTP 401.",
    ),
    # State Medicaid FFS provider directories from the CMS SMA-Endpoint-Directory
    # (github.com/CMSgov/SMA-Endpoint-Directory). Reachable and returning Bundles, but
    # per-NPI search returns empty even for listed providers, so they can't confirm an
    # individual NPI. Tracked as targets; the ~14 api.<state>fhir.com hosts were
    # unreachable from the validation environment (likely network/geo-restricted).
    PlanNetEndpoint(
        id="wa_medicaid", label="Washington Apple Health (Medicaid) directory",
        base_url="https://wa.fhir.mhbapp.com/pd/api/v1", category="medicaid",
        states=["WA"], status="unusable",
        note="Returns a Bundle but practitioner.identifier search returns 0 roles even "
             "for a listed NPI — not usable for per-NPI verification.",
    ),
    # National commercial: its public FHIR /metadata responds, but /PractitionerRole
    # times out from the current validation environment (slow or geo-restricted). A
    # strong candidate — re-check `python -m app.verify_payers` from an unrestricted
    # network (or the deployed box); it graduates the catalog `humana` estimate the
    # moment it passes the round-trip.
    PlanNetEndpoint(
        id="humana", label="Humana",
        base_url="https://fhir.humana.com/api", category="commercial",
        states=None, status="unreachable",
        note="Public FHIR base reachable (/metadata 200) but /PractitionerRole read-times-out "
             "from the validation environment; not yet round-trip-verified. Re-check from an "
             "unrestricted network.",
    ),
]


def validated() -> list[PlanNetEndpoint]:
    """Endpoints that passed the full per-NPI round-trip — safe to wire as verified."""
    return [e for e in REGISTRY if e.status == "validated"]


def validated_payer_configs() -> list[dict[str, Any]]:
    """Validated endpoints as payers.json-shaped configs, for Registry.build(). Empty
    when CAREFIND_USE_PLANET_REGISTRY is off (e.g. hermetic tests opt in explicitly)."""
    if not settings.use_planet_registry:
        return []
    return [e.payer_config() for e in validated()]
