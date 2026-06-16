"""Insurance taxonomy + the curated payer catalog.

Two things live here:

  1. CATEGORIES — the canonical coverage *types* CareFind groups payers under, in
     display order. Every plan belongs to exactly one category.

  2. PAYER_CATALOG — a curated list of major US payers used for the ESTIMATED tier.
     These are not per-provider verified facts; they let CareFind offer a broad,
     recognizable set of named-payer filters. When no verified source covers an
     (NPI, payer), a catalog payer that operates in the provider's state is surfaced
     as "Estimated" ("likely — confirm with the provider"), never as confirmed.

     `states: None` means the payer operates nationally. A state list means it is
     regional and only estimated for providers located in those states.

Verified sources (Medicare file, FHIR Plan-Net endpoints, Transparency-in-Coverage
ingests) always take precedence over a catalog estimate for the same payer id.
"""
from typing import Any

# (id, label) in display order. Keep ids stable — they appear in the API + URLs.
CATEGORIES = [
    ("medicare", "Medicare"),
    ("medicare_advantage", "Medicare Advantage"),
    ("medicaid", "Medicaid"),
    ("commercial", "Commercial / Employer"),
    ("marketplace", "ACA Marketplace"),
    ("tricare", "TRICARE"),
    ("va", "VA"),
]
CATEGORY_LABELS = dict(CATEGORIES)
CATEGORY_ORDER = {cid: i for i, (cid, _) in enumerate(CATEGORIES)}


def category_label(cid: str) -> str:
    return CATEGORY_LABELS.get(cid, cid.replace("_", " ").title())


# Curated estimated-tier payers. `states=None` -> national.
PAYER_CATALOG: list[dict[str, Any]] = [
    # ── National commercial / employer ──
    {"id": "unitedhealthcare", "label": "UnitedHealthcare", "category": "commercial", "states": None},
    {"id": "aetna", "label": "Aetna", "category": "commercial", "states": None},
    {"id": "cigna", "label": "Cigna", "category": "commercial", "states": None},
    {"id": "humana", "label": "Humana", "category": "commercial", "states": None},
    {"id": "bcbs", "label": "Blue Cross Blue Shield", "category": "commercial", "states": None},
    {"id": "anthem", "label": "Anthem", "category": "commercial",
     "states": ["CA", "CO", "CT", "GA", "IN", "KY", "ME", "MO", "NV", "NH", "NY", "OH", "VA", "WI"]},
    {"id": "kaiser", "label": "Kaiser Permanente", "category": "commercial",
     "states": ["CA", "CO", "GA", "HI", "MD", "OR", "VA", "WA", "DC"]},
    {"id": "centene", "label": "Centene / Ambetter", "category": "commercial", "states": None},
    {"id": "molina", "label": "Molina Healthcare", "category": "commercial", "states": None},
    {"id": "oscar", "label": "Oscar Health", "category": "commercial", "states": None},

    # ── Regional payers with live-validated public FHIR Plan-Net directories ──
    # State-scoped to exactly where they operate (NOT national — Premera is a WA/AK
    # BCBS licensee, not Blue Cross nationally). Estimated by default; they graduate
    # to a verified green filter automatically when wired in payers.json (the ids
    # match payers.example.json). See the README's validated-endpoints table.
    {"id": "premera_bcbs", "label": "Premera Blue Cross (WA/AK)", "category": "commercial", "states": ["WA", "AK"]},

    # ── Medicaid managed-care (regional) ──
    {"id": "priority_partners", "label": "Priority Partners (MD Medicaid)", "category": "medicaid", "states": ["MD"]},

    # ── Medicare Advantage (national carriers that sell MA plans) ──
    {"id": "uhc_ma", "label": "UnitedHealthcare (Medicare Advantage)", "category": "medicare_advantage", "states": None},
    {"id": "humana_ma", "label": "Humana (Medicare Advantage)", "category": "medicare_advantage", "states": None},
    {"id": "aetna_ma", "label": "Aetna (Medicare Advantage)", "category": "medicare_advantage", "states": None},

    # ── ACA Marketplace ──
    {"id": "ambetter", "label": "Ambetter (Marketplace)", "category": "marketplace", "states": None},
    {"id": "oscar_marketplace", "label": "Oscar (Marketplace)", "category": "marketplace", "states": None},

    # ── Public / other ──
    {"id": "medicaid", "label": "Medicaid", "category": "medicaid", "states": None},
    {"id": "tricare", "label": "TRICARE", "category": "tricare", "states": None},
    {"id": "va", "label": "VA Community Care", "category": "va", "states": None},
]
