"""Verified-coverage-by-state report (C4).

Makes the verified tier *visible*: which verified programs (Medicare, ingested TiC
payers, validated FHIR Plan-Net endpoints) are available in each state, plus the raw
verified-NPI counts behind them. Computed live from the registry + datastore, so it
always reflects the current data — it "regenerates" on every ingest automatically.

Deliberately honest about what it is NOT: it is *availability of verified programs by
state* and *counts of verified records*, not a "% of providers covered" — CareFind
holds no per-state provider denominator (NPPES is queried live), so a coverage
percentage would be fabricated. We report only what the data supports.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from . import db

if TYPE_CHECKING:
    from .insurance import Registry

# The states/territories CareFind recognizes (mirrors the frontend's US_STATES).
STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
)


def coverage_report(reg: Registry) -> dict[str, Any]:
    """Verified programs available per state + the verified-record counts behind them."""
    verified = [s for s in reg.available() if s.confidence == "verified"]

    programs: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for s in verified:
        scope = sorted(s.states) if s.states else None  # None -> national
        programs.append({"id": s.id, "label": s.label, "category": s.category,
                         "level": s.level, "scope": scope})
        # Record counts we actually hold locally (Medicare file, TiC ingests). Live FHIR
        # sources have no local NPI count, so they're reported as available (no count).
        if s.id == "medicare":
            counts[s.id] = db.medicare_count()
        elif db.tic_count(s.id) > 0:
            counts[s.id] = db.tic_count(s.id)

    by_state: dict[str, list[str]] = {}
    for st in STATES:
        by_state[st] = [s.id for s in verified if s.states is None or st in s.states]

    states_with_coverage = sum(1 for ids in by_state.values() if ids)
    return {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "verified_program_count": len(programs),
        "states_with_verified_coverage": states_with_coverage,
        "verified_programs": programs,
        "verified_counts": counts,
        "by_state": by_state,
    }
