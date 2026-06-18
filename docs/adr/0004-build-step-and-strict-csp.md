# ADR 0004 — esbuild bundle + external config + strict CSP

**Status:** accepted

## Context
The frontend began as a ~1,100-line inline `<script>` in carefind.html. Inline script
forces `script-src 'unsafe-inline'` (an XSS foothold) and isn't unit-testable.

## Decision
Author the frontend as ES modules under `src/`, bundled by esbuild (pinned) into a single
committed `carefind.bundle.js`; deploy config lives in an external `carefind.config.js`.
The page therefore carries **no inline executable script**, so the CSP `script-src` drops
`'unsafe-inline'`. The build is reproducible — CI rebuilds and fails on any diff. The pure,
shared logic stays in the unit-tested `carefind.logic.js`.

## Consequences
- Strict CSP for scripts; an injected inline `<script>` won't run.
- The "one HTML file + bundle + backend" deploy story is preserved (the bundle is a build
  artifact). `style-src` keeps `'unsafe-inline'` (per-provider colors + Leaflet) — a
  documented, lower-risk residual.
