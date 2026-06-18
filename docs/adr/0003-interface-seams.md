# ADR 0003 — External dependencies behind Protocols (scale by config, not rewrite)

**Status:** accepted

## Context
The $0 defaults (SQLite, in-process cache + rate limiter) are correct for a single worker
but not for multi-worker/HA. We don't want a rewrite when scaling.

## Decision
Put every external dependency behind a `typing.Protocol` in `app/interfaces.py`:
`Datastore`, `CacheBackend`, `RateLimiter`, `GeocoderBackend`. The shipped SQLite/in-proc
implementations satisfy them; a `build_*` factory selects the implementation from config
(`CAREFIND_DATASTORE`/`_RATE_LIMITER`/`_CACHE`). Conformance is asserted by tests and by
mypy. Scaling to Postgres/Redis is a new class that satisfies the same Protocol + a config
value — no call-site change.

## Consequences
- Defaults are unchanged and fully tested; alternates are a drop-in.
- The seam is real (the rate limiter is injected; the datastore is swappable in a test),
  not aspirational. Postgres/Redis drivers are added when a deployment needs them.
