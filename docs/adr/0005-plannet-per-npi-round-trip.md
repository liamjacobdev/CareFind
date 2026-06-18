# ADR 0005 — A Plan-Net endpoint is "validated" only via the per-NPI round-trip

**Status:** accepted

## Context
Wiring a public FHIR Plan-Net directory as a *verified* source is only safe if it answers
the exact per-NPI lookup CareFind performs — truthfully. Live testing revealed two failure
modes among real public directories: some **ignore the NPI filter** (a bogus NPI returns
thousands of providers → would mark everyone in-network), and some **return nothing for a
listed NPI** (→ would mark everyone not-in-network). A "returns a Bundle" check misses both.

## Decision
`app/verify_payers.py` validates an endpoint by running CareFind's own `_in_network`
determination over the real lookup: a **bogus** NPI must not resolve in-network, **and** a
**real listed** NPI (discovered from the directory) must resolve in-network. Only endpoints
passing both are wired (`app/planet_registry.py`); others are tracked as gated/unusable
with a reason. The nightly job re-runs this against the live endpoints.

## Consequences
- The verified tier cannot fabricate a yes or a no, even as payer endpoints change.
- The freely-validatable, NPI-usable public set is small today (honest), but grows
  automatically as endpoints start passing — never by assertion.
