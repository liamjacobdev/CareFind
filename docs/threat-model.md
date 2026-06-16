# CareFind — threat model

Scope: the CareFind frontend (static page + bundle) and the FastAPI backend that proxies
NPPES, geocoding, and resolves insurance. $0, self-hostable, single-tenant by default.

## Assets
- **Users' search activity** (a ZIP, a provider name, "near me" coordinates) — privacy
  sensitive: it can reveal where someone is and what care they're looking for.
- **Availability** of the proxy (it shields the public registries from abuse).
- **Integrity of "Confirmed" claims** — the product's core promise. A fabricated verified
  badge is the single most harmful failure (see the trust rules; A1–A5, C1).
- **Operator secrets** — `CAREFIND_ADMIN_TOKEN` (ingest/metrics), any payer API keys.

## Trust boundaries
1. Browser ↔ backend (same-origin in the shipped deploy; CORS-locked otherwise).
2. Backend ↔ upstreams (NPPES, Census/Nominatim, payer FHIR Plan-Net) — untrusted input.
3. CI ↔ deployed backend (the scheduled-ingest cron calls a token-secured endpoint).
4. Edge (Caddy/TLS) ↔ backend (the proxy sets X-Forwarded-For; trusted only when
   `CAREFIND_TRUST_PROXY=true`).

## Threats & mitigations (STRIDE-ish)

| Threat | Mitigation |
|--------|------------|
| **XSS / script injection** into the page | CSP `script-src` has **no `'unsafe-inline'`** — all JS is same-origin (config + bundle) or the pinned Leaflet CDNs; injected inline `<script>` won't execute. `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'self'`. Output is escaped (`esc`/`cssEsc`). |
| **Clickjacking** | `frame-ancestors 'self'` + `X-Frame-Options: DENY`. |
| **Exfiltration** of data from a content bug | CSP `connect-src`/`img-src` locked to known origins, so a bug can't beacon to an arbitrary host. |
| **MITM / downgrade** | HSTS (`max-age=63072000; includeSubDomains; preload`) at the edge; TLS via Caddy/Let's Encrypt. |
| **Abuse of the open proxy** (driving NPPES/Nominatim load) | Per-client rate limiting (bounded, swept); input-length caps on every query field; bounded geocode batch + budget; bounded ingest downloads. |
| **Disclosure of users' search activity** (PII) | Search terms, the upstream URL, and client IPs are **never persisted to logs**: failure logs record only which fields were present + the error type; the access log logs the path (no query string); httpx request logging is pinned to WARNING; the rate-limit log drops the IP. No search history is stored server-side. Enforced by `test_no_pii_in_logs`. |
| **Tampering with verified claims** | Verified answers require a real source for *that* provider with provenance (`source_url` + `fetched_at`); the Plan-Net validator only wires an endpoint that passes the per-NPI round-trip; "unknown" never becomes "yes" (trust-rule tests, A5/C1). |
| **Unauthorized ingest / metrics scraping** | `POST /admin/ingest` is disabled until `CAREFIND_ADMIN_TOKEN` is set, then requires `Authorization: Bearer <token>`; `/metrics` requires the same token when configured. |
| **IP spoofing to dodge the rate limit** | `X-Forwarded-For` is trusted only when `CAREFIND_TRUST_PROXY=true` (behind a controlled proxy); otherwise the direct peer is used. |
| **Supply-chain (deps / actions)** | pip-audit + `npm audit --omit=dev` + CodeQL + gitleaks in CI; Dependabot for pip/npm/actions; esbuild pinned; the committed bundle is rebuilt + diff-checked in CI. |
| **SSRF via an operator-supplied ingest/payer URL** | Ingest downloads are streamed and hard-capped (`ingest_max_bytes`); these URLs are operator-configured (not user input). Operators should point them only at trusted published files. |
| **Secrets in the repo** | gitleaks in CI; secrets come from env (`CAREFIND_*`) / repo secrets, never committed. |

## Residual risks (accepted, documented)
- **`style-src 'unsafe-inline'`** remains: per-provider marker/avatar colors and Leaflet
  set inline `style` attributes. Style injection is far lower-risk than script injection;
  removing it would require eliminating all dynamic inline styles. Tracked as future work.
- **Third-party CDNs** (Leaflet, fonts, map tiles) are trusted origins in the CSP; a
  compromise there is out of scope for a $0 self-host. Self-hosting these assets would
  remove the dependency (future work).
- **Screen-reader transcripts** (NVDA/VoiceOver) are captured manually by the maintainer;
  automated axe + keyboard checks gate CI (see docs/a11y-walkthrough.md).
