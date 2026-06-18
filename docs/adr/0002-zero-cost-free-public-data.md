# ADR 0002 — $0 forever, free public data only

**Status:** accepted

## Context
CareFind must be a durable, free public good. Paid data/eligibility APIs exist but would
make the project non-free and non-reproducible for others.

## Decision
Use only free, durable sources: the CMS Medicare enrollment file, CMS-mandated public
FHIR Plan-Net `PractitionerRole` directories, and public Transparency-in-Coverage files —
plus keyless geocoding (US Census, then Nominatim). No clearinghouse/270-271 or licensed
payer feeds. Infra is free-tier: SQLite + a single container behind Caddy, GitHub Actions
cron for ingestion, Healthchecks.io/UptimeRobot for monitoring. If a task seems to need
money, find the free path or redefine the task.

## Consequences
- Anyone can self-host the whole thing at no cost.
- The verified-coverage ceiling is bounded by what's freely public (small today) — we are
  honest about that rather than buying coverage.
- Scaling beyond a single box is enabled by interface seams (ADR 0003), not by spend.
