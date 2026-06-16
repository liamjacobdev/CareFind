"""Parse a Transparency-in-Coverage table-of-contents (index) file.

A payer's published TiC root is usually a *table-of-contents* JSON, not the in-network
file itself: it lists, per reporting structure, the in-network file URLs plus the plans
they cover. This module discovers those in-network file URLs (with their plan metadata)
so the ingest can fan out across a payer's many files automatically, instead of needing
each in-network URL hand-configured.

A ToC is identified by a top-level `reporting_structure`. A file *without* it (e.g. one
whose top level is `in_network`) is an in-network file, not an index — `parse_index`
returns `[]` for it so the caller falls back to ingesting it directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class InNetworkFileRef:
    """One discovered in-network file: its URL and the plan ids it covers (when the
    index exposes them — plan granularity, used for provenance/future plan-level ingest)."""

    location: str
    description: str = ""
    plan_ids: tuple[str, ...] = field(default_factory=tuple)


def _plan_ids(reporting_plans: object) -> tuple[str, ...]:
    out: list[str] = []
    if isinstance(reporting_plans, list):
        for p in reporting_plans:
            if isinstance(p, dict):
                pid = p.get("plan_id") or p.get("plan_name")
                if pid:
                    out.append(str(pid))
    return tuple(out)


def parse_index(raw: bytes) -> list[InNetworkFileRef]:
    """Return the in-network file references in a TiC ToC. `[]` when `raw` is not a ToC
    (no `reporting_structure`) — i.e. it's already an in-network file."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    structures = data.get("reporting_structure")
    if not isinstance(structures, list):
        return []

    refs: list[InNetworkFileRef] = []
    seen: set[str] = set()
    for struct in structures:
        if not isinstance(struct, dict):
            continue
        plan_ids = _plan_ids(struct.get("reporting_plans"))
        for f in struct.get("in_network_files", []) or []:
            if not isinstance(f, dict):
                continue
            loc = (f.get("location") or "").strip()
            if loc and loc not in seen:
                seen.add(loc)
                refs.append(InNetworkFileRef(
                    location=loc, description=str(f.get("description", "")), plan_ids=plan_ids))
    return refs
