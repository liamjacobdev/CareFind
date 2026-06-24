# CareFind — architecture

CareFind answers one question — *"which licensed providers near me take my insurance,
and can I act on it now?"* — from **free, public data only**, and never claims more than
a real source supports.

## Components

```
                    ┌─────────────────────────────────────────────┐
   Browser          │  carefind.html  (CSP, no inline script)      │
   (PWA, offline    │  ├─ carefind.config.js   (injected config)   │
    shell via sw.js)│  ├─ carefind.bundle.js   (esbuild ← src/)    │
                    │  └─ carefind.logic.js    (pure, unit-tested) │
                    └───────────────┬─────────────────────────────┘
                                    │ same-origin /api/* (CORS-locked, rate-limited)
                    ┌───────────────▼─────────────────────────────┐
                    │  FastAPI backend (app/)                      │
                    │  main.py  — routes, middleware, /healthz,    │
                    │             /readyz, /metrics, /coverage     │
                    │  insurance.py — two-tier confidence model    │
                    │  nppes.py · geocode.py — upstream proxies    │
                    │  planet_registry.py · verify_payers.py       │
                    │  ── seams (interfaces.py) ──                 │
                    │  Datastore · CacheBackend · RateLimiter ·    │
                    │  GeocoderBackend                             │
                    └───┬───────────┬───────────┬─────────────┬────┘
                        │           │           │             │
                  ┌─────▼────┐ ┌────▼─────┐ ┌───▼──────┐ ┌────▼─────────┐
                  │ SQLite   │ │  NPPES   │ │ Census / │ │ FHIR Plan-Net│
                  │ (db.py)  │ │ registry │ │Nominatim │ │  + TiC files │
                  └──────────┘ └──────────┘ └──────────┘ └──────────────┘
                   datastore     providers    geocoding    verified insurance
```

## Data flow (a search)
1. The page calls `GET /api/providers/search` (same origin).
2. `nppes.search` queries the NPPES registry (cached, retried, timeout-bounded) → providers.
3. `insurance.Registry.annotate` tags each provider per plan with `{value, confidence,
   level, source, source_url?, fetched_at?}` — **verified** (a real source for that NPI)
   or **estimated** (a clearly-labeled catalog guess). Verified always wins; "unknown"
   is never turned into a yes.
4. For a radius search the backend geocodes the candidate pool, keeps only those within
   `radius` miles of the ZIP centroid, sorts by distance, then truncates — the backend is
   authoritative for the boundary.
5. The page renders cards + a map; verified hits show provenance ("Verify · checked <date>").

## The two-tier confidence model (the heart)
- **verified** — Medicare enrollment file (national), an ingested Transparency-in-Coverage
  in-network file (by NPI), or a validated public FHIR Plan-Net directory (per-NPI, network
  linked). Carries `{source, source_url, fetched_at}`. A green badge is always traceable.
- **estimated** — a curated major payer that operates in the provider's state. Hidden by
  default; shown only via "Include estimated" and labeled "likely — confirm". National
  estimates that match everyone in-state are honestly framed as *area context*, not a match.

Trust invariants are executable: `tests/test_trust_rules.py` asserts no path turns
unknown→yes, estimates never render Confirmed, and verified results always carry provenance.

## Scale-readiness seams (interfaces.py)
Every external dependency sits behind a Protocol so scaling is a **config swap**, not a
rewrite: `Datastore` (SQLite→Postgres), `CacheBackend` (in-proc→Redis), `RateLimiter`
(per-worker→shared), `GeocoderBackend`. Upstreams are timeout-bounded + retried, and
degrade to "unknown" (never a fabricated answer) on failure.

## Deploy
One HTML file + the built bundle + the FastAPI backend, behind Caddy (TLS + the
authoritative security headers). Ingestion is a free GitHub Actions cron hitting a
token-secured endpoint; `/healthz` enforces data-age SLOs. See [docs/runbook.md](runbook.md).
