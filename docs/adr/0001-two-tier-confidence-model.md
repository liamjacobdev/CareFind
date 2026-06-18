# ADR 0001 — Two-tier confidence model (verified vs estimated)

**Status:** accepted

## Context
There is no single free API that says "provider X takes plan Y." We have free, public
sources of varying strength (Medicare enrollment, FHIR Plan-Net directories, TiC files)
and a desire to still show broad, recognizable payer filters on day one. The overriding
risk in a health tool is telling a patient a provider takes their insurance when they
don't — an active harm.

## Decision
Every insurance answer carries a `confidence`: **verified** (a real source for *that* NPI,
with `{source, source_url, fetched_at}`) or **estimated** (a curated catalog guess that a
payer operates in the provider's state). Verified always wins over estimated; a source may
answer True/False/None and **None ("unknown") is never turned into a yes**. Estimated is
hidden by default and, when shown, labeled "likely — confirm," never "Confirmed". The
invariants are enforced by executable tests (`tests/test_trust_rules.py`).

## Consequences
- A green badge is always traceable to a real source + date.
- Coverage breadth grows by adding verified sources, not by loosening claims.
- "Category 2 is 10/10" is defined as *maximal verified coverage from free data + zero
  overclaim + full provenance*, not nationwide real-time eligibility (which isn't free).
