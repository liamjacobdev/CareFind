# Load & chaos testing (D4)

These are run by an operator against a deployed instance (a build sandbox has no load
tooling). They verify the resilience the in-process tests already prove unit-by-unit:
the circuit breakers + caches keep latency bounded and avoid a 5xx pile-up under load.

## Load — p95 target

[k6](https://k6.io) is a free, single-binary tool:

```bash
BASE=https://api.yourdomain.com k6 run loadtest/k6_search.js
```

Pass criteria (thresholds in the script): **p95 < 800ms** and **<1% failed requests**
under 25 concurrent users. Run with 4 uvicorn workers (`uvicorn app.main:app --workers 4`)
to confirm multi-worker correctness; the shared-state seams (rate limiter, cache,
datastore) are behind the B2 protocols, so a single global limit/cache across workers is
a config swap to a Redis/Postgres backend (see app/interfaces.py) — no code change.

## Chaos — zero 5xx pile-up

The circuit breakers (app/circuit.py) are exercised in CI by `tests/test_resilience.py`
(NPPES + FHIR outages fast-fail and degrade to unknown, never a 500 or a fabricated
answer). To reproduce live, point an upstream at an unreachable host (e.g. set
`NPPES_BASE` to a black-hole address) and run the k6 script: requests should degrade
(a controlled 502 for search, "unknown" insurance, no coordinates) rather than hang, and
recover automatically once the upstream returns.
