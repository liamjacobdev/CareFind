# CareFind — Execution Plan to 10/10 (for Claude Opus 4.8)

> **This document is self-contained.** A fresh session can execute it with no prior
> context. It is the single source of truth for driving CareFind to a verifiable
> 10/10 across all ten audit categories. Read "Operating Principles" before touching
> any code, then execute phases **A → E in order**. Each task has a binary
> **acceptance gate**; a task is "done" only when its gate passes in CI or via a
> documented, repeatable command.

---

## 0. Mission & Hard Constraints

**Mission.** Make CareFind the most trustworthy, broadly-useful free tool for
answering: *"Which licensed providers near me take my insurance — and can I act on
it right now?"*

**Hard constraints (non-negotiable):**
- **$0 forever.** No paid services, data, or infra. Free tiers only, and only where
  a free tier is durable. If a task seems to need money, find the free path or
  redefine the task — never introduce a cost.
- **Public/free data sources only.** Medicare enrollment file, CMS-mandated public
  FHIR Plan-Net `PractitionerRole` directories, and public Transparency-in-Coverage
  (TiC) files. No clearinghouse/eligibility (270/271) APIs, no licensed payer feeds.
- **Never overclaim.** A green "Confirmed" badge must be traceable to a real source
  for *that* provider, with a fetch date. Absence of data is "unknown," never "no"
  and never a fabricated "yes." This invariant outranks every feature.
- **Single-file deploy preserved.** The app still ships as `carefind.html` (+ the
  served logic). A build step may *produce* that file, but the deploy story stays
  "one HTML file + a FastAPI backend."
- **Scale-ready by abstraction, not by spend.** Every external dependency (datastore,
  cache, rate limiter, geocoder) sits behind an interface so scaling is a config
  swap. We build to the best self-hosted bar; we do not pay for HA.

**Stack today:** FastAPI backend (`app/`), single-file frontend (`carefind.html` +
`carefind.logic.js`), SQLite, Leaflet map, Pytest + Vitest + Playwright, Docker +
Caddy deploy. Python suite currently 57/57 green.

---

## 1. Operating Principles (read every session)

1. **Trust is the product.** Before merging anything, ask: "Can this make a badge or
   claim that the data doesn't support?" If yes, stop.
2. **One task = one PR = one commit**, following the repo convention
   (`A1: <imperative summary>`). Tests land with the change, never after.
3. **Every PR is green**: `pytest -q`, the JS suites, and all CI gates pass before
   merge. Never weaken a gate to pass; fix the cause.
4. **No new untested logic.** If you add a branch, you add a test for it.
5. **Honesty in docs.** Keep `Readme.md`'s "what's verified vs. what needs your
   network" current with reality. Update the provenance ledger when sources change.
6. **Prefer deletion.** The cleanest win is removing a misleading affordance, not
   adding a caveat to it.
7. **Verify, don't assert.** When you claim something works, show the passing
   gate/output. Run the app (uvicorn) and exercise the path when behavior is
   observable.

---

## 2. The Definition of "10" — per-category acceptance gates

A category is 10/10 **only** when its gate is objectively satisfied. This table is
the final re-audit checklist (Phase E re-runs all of it).

| # | Category | Gate (must all pass) |
|---|----------|----------------------|
| 1 | Product & Value | Golden-journey e2e (plan→ZIP→specialty→verified provider in <30s, <5 interactions) green; committed head-to-head doc beating NPPES site + 2 insurer directories on speed/privacy/verified breadth; Lighthouse Best-Practices + SEO ≥ 98. |
| 2 | Insurance Integrity | ≥40 live-validated public payer endpoints wired out of the box; 100% of Confirmed claims carry `{source, source_url, fetched_at}` (test-enforced); automated proof presence-only/`unknown` can never render Confirmed; verified-%-by-state dashboard regenerated each ingest. |
| 3 | Architecture | mypy `--strict` + ruff + eslint + prettier clean in CI; no logic file >400 lines; no import cycles; 100% public functions documented; external deps behind interfaces. |
| 4 | Backend Reliability | Correct with 4 workers (shared rate-limit + metrics); load test meets p95 target; chaos test (killed upstream) leaks zero 5xx; 100% upstream calls bounded+retried+circuit-broken. |
| 5 | Frontend/UX | Lighthouse ≥98 all four categories; 1,000-result list scrolls at 60fps (profiled); installs as PWA and serves a cached search offline; CLS ≈ 0. |
| 6 | Accessibility | axe-core 0 violations on every view AND state; WCAG 2.2 AA checklist 100%; keyboard-only completes golden journey; committed NVDA + VoiceOver walkthrough. |
| 7 | Security/Privacy | CodeQL + gitleaks + pip-audit + npm audit clean in CI; securityheaders.com A+; CSP has no `unsafe-inline`; committed threat model; test proving no PII in persistent logs. |
| 8 | Testing/QA | ≥90% line+branch both suites; mutation score ≥ target; e2e green on chromium+firefox+webkit; nightly live-integration job green; every fix has a regression test. |
| 9 | Data/Operations | Fresh clone → deployed + ingesting on a free tier with zero manual data steps; dead-man's-switch fires on stalled ingest; restore-from-backup tested + documented; data-age SLOs enforced in `/healthz`. |
| 10 | Documentation | Validated zero→running in <15 min from docs; docs link-checked in CI; OpenAPI published; provenance ledger auto-current; executable "trust-rule" tests passing. |

---

## 3. Phased Execution

Execute **A → E in order.** Within a phase, tasks may parallelize unless a dependency
is noted. Each task: **Goal · Files · Steps · Gate · Commit.**

---

### PHASE A — Trust (safety-critical; do first, blocks everything)

> A wrong "Confirmed" badge is the only failure that can actively harm a patient.
> Nothing else ships until this is airtight.

#### A1 — Tighten the FHIR "in-network" determination
- **Goal:** Directory *presence* must not become "Confirmed." Require an active
  `PractitionerRole` that links to a network for the queried payer; otherwise return
  `unknown` (None), never True.
- **Files:** `app/insurance.py` (`FhirPlanNetSource._in_network`, ~L173-189; `_check`,
  `check_many`), `tests/test_insurance.py`.
- **Steps:**
  1. Rewrite `_in_network()` to require `active is not False` AND a resolvable
     `network` reference (not merely `healthcareService` presence). Add a config flag
     `CAREFIND_FHIR_STRICTNESS = network|directory` (default `network`).
  2. Map "listed but no network link" to `None` (unknown), not `True`.
  3. Carry source provenance through the result (see A3).
- **Gate:** New tests assert (a) active + network → True, (b) presence-only → None,
  (c) `active:false` → not True, (d) strictness flag toggles behavior. `pytest -q` green.
- **Commit:** `A1: require network linkage for FHIR Confirmed; presence-only is unknown`

#### A2 — Model `payer` vs `plan` explicitly
- **Goal:** Stop reading a payer-level hit as a plan-level confirmation. "Aetna network
  directory" ≠ "Aetna Gold PPO accepted."
- **Files:** `app/catalog.py`, `app/insurance.py` (sources, `Registry.plans`,
  `annotate`), `app/main.py` (`/api/insurance/plans` shape), `carefind.logic.js`,
  `carefind.html` (`renderInsuranceFilter`, `coverageHtml`, `insuranceBadgesHtml`),
  tests in both suites.
- **Steps:**
  1. Add `level: "payer" | "plan"` to every source and to each emitted plan dict.
  2. UI renders payer-level results as *"listed in <payer> network directory"* and
     plan-level as the specific plan name. Badges and the coverage drawer must visibly
     distinguish them.
  3. Update the golden fixture (`tests/fixtures/normalize_golden.json`) only if shape
     changes; keep Python↔JS parity (`tests-js/parity.test.js`).
- **Gate:** `/api/insurance/plans` returns `level` per plan; UI test proves payer-level
  copy never says "accepts your plan"; both suites green.
- **Commit:** `A2: distinguish payer-level network listing from plan-level acceptance`

#### A3 — Provenance on every verified answer
- **Goal:** Every Confirmed badge is traceable: source id, source URL, fetch date.
- **Files:** `app/insurance.py` (result shape `{value, confidence, source}` →
  `+ source_url, fetched_at, level`), `app/db.py` (FHIR/TiC rows already store
  `fetched_at`; expose it), `carefind.html` (`coverageHtml` adds a "verify (as of
  <date>)" link to the payer's own directory).
- **Steps:**
  1. Thread `source_url` + `fetched_at` from the cache rows into `annotate()` output.
  2. Render a per-result "Verify with <payer> · checked <date>" deep link.
- **Gate:** Test: 100% of `value:true, confidence:verified` results include non-empty
  `source_url` and `fetched_at`. UI shows the verify link.
- **Commit:** `A3: attach source URL + fetch date to every verified result`

#### A4 — Fix the noise-tier estimate
- **Goal:** An estimate of "national payer operates in this state" matches every
  in-state provider and doesn't filter. Stop presenting that as a meaningful signal.
- **Files:** `app/insurance.py` (`EstimatedPayerSource`, ~L262-292), `app/catalog.py`,
  `carefind.html` (`renderInsuranceFilter`, `estimatedFilterHint`).
- **Steps:**
  1. For **national** payers, drop pure state-presence estimates from the *filter*
     (they don't narrow). Keep estimates only where state genuinely discriminates
     (regional payers) OR relabel as non-filtering context "Operates in your area."
  2. Ensure verified-by-default remains: estimates never render as Confirmed.
- **Gate:** Test: selecting an estimated national payer no longer returns "all
  in-state providers" as a filtered set; UI explains the estimated tier honestly.
- **Commit:** `A4: stop treating national "operates in-state" as a filtering estimate`

#### A5 — Encode the honesty invariant as executable trust-rule tests
- **Goal:** The "never overclaim" rule is enforced by tests, not just prose.
- **Files:** `tests/test_trust_rules.py` (new), `CONTRIBUTING.md`.
- **Steps:** Property/parametrized tests asserting: no code path turns `None`/unknown
  into `True`; estimated confidence can never emit a Confirmed badge; verified results
  always carry provenance. Reference these from CONTRIBUTING's "trust rules."
- **Gate:** `tests/test_trust_rules.py` green and run in CI.
- **Commit:** `A5: enforce the verified-vs-estimated honesty invariant in tests`

---

### PHASE B — Foundation (unlocks categories 3, 7, 8 and the seams for 4)

#### B1 — Introduce a $0 build step (esbuild) + module decomposition
- **Goal:** Kill the ~1,100-line inline `<script>` in `carefind.html` (≈L561-1686);
  produce the single-file artifact from modules. This unblocks strict CSP (B-dep for
  #7) and UI unit testing (#8).
- **Files:** new `src/` (`net.js`, `state.js`, `map.js`, `search.js`, `ui.js`,
  `main.js`), `build.mjs` (esbuild), `package.json` (add `build` script; esbuild is a
  free devDependency), `carefind.html` (template with an injected bundle + nonce),
  `configure_frontend.py` (emit config + nonce).
- **Steps:**
  1. Extract the inline script into ES modules under `src/`; keep `carefind.logic.js`
     as the shared pure module (already tested).
  2. `build.mjs` bundles to a hashed asset and inlines/links it into `carefind.html`
     with a CSP nonce; `npm run build` is reproducible and committed-output verified
     in CI.
  3. Move `API_BASE`/`CLAIM_EMAIL` to injected `window.CAREFIND_CONFIG` (remove
     hardcoded `localhost:8000` at `carefind.html:574`).
- **Gate:** `npm run build` reproduces the artifact deterministically (CI diff check);
  app loads and golden journey passes; no inline business logic remains in the HTML.
- **Commit:** `B1: add esbuild build step; split inline UI script into tested modules`

#### B2 — Define interface seams for scale-readiness
- **Goal:** Datastore, cache, rate limiter, and geocoder behind protocols so scaling
  is config, not rewrite.
- **Files:** `app/interfaces.py` (new Protocols), `app/db.py`, `app/geocode.py`,
  `app/main.py` (rate limiter), `app/metrics.py`.
- **Steps:** Extract `Datastore`, `CacheBackend`, `RateLimiter`, `GeocoderBackend`
  protocols; make current SQLite/in-proc implementations satisfy them; wire selection
  via config with SQLite/in-proc defaults.
- **Gate:** Existing tests pass against the interface; a no-op alternate impl can be
  swapped in a test. No behavior change by default.
- **Commit:** `B2: extract datastore/cache/ratelimit/geocoder behind interfaces`

#### B3 — CI quality + security gates
- **Goal:** Make quality non-negotiable and automated.
- **Files:** `.github/workflows/ci.yml`, `pyproject.toml`/`ruff.toml`, `.eslintrc`,
  `.prettierrc`, `mypy.ini`.
- **Steps:** Add ruff + black + mypy `--strict` (py), eslint + prettier + `tsc
  --checkJs` (js); add CodeQL, Dependabot, gitleaks, `pip-audit`, `npm audit` jobs
  (all free for public repos). Gate the build on all of them.
- **Gate:** All linters/type-checkers/scanners clean and required in CI.
- **Commit:** `B3: enforce lint, strict types, and $0 security scanning in CI`

---

### PHASE C — The verified-coverage payload (the heart of #2, plus #9, #1)

#### C1 — Public FHIR Plan-Net endpoint registry + validator
- **Goal:** Wire the large free universe of CMS-mandated public `PractitionerRole`
  directories (Medicare Advantage, Medicaid, CHIP, ACA Marketplace/QHP issuers — all
  required to publish unauthenticated under CMS-9115-F).
- **Files:** `app/planet_registry.py` (new), `payers.json` seed, `app/verify_payers.py`
  (new CLI), `Readme.md` validated-endpoints table.
- **Steps:**
  1. Curate a seed list of public, correctly **state-scoped** Plan-Net base URLs
     (never map a regional licensee to a national catalog id).
  2. `verify_payers` live-checks each (returns a real `PractitionerRole` Bundle,
     no auth), records Bundle total + ISO date, and writes the provenance ledger.
  3. Map each validated endpoint to a correctly-scoped catalog id; it graduates to a
     verified filter automatically (existing registry behavior).
- **Gate:** ≥40 endpoints validated and wired out of the box; `verify_payers` output
  committed as the provenance ledger with dates.
- **Commit:** `C1: validated registry of public FHIR Plan-Net endpoints + verifier`

#### C2 — Transparency-in-Coverage index auto-discovery + scaled ingest
- **Goal:** The free path to commercial/employer coverage. Parse public TiC
  table-of-contents index files to discover each payer's in-network file, then ingest
  NPIs (idempotent monthly job already exists).
- **Files:** `app/tic_index.py` (new), `app/ingest_tic.py`, `app/ingest_tic_job.py`,
  `tic_sources.json` seed, tests + a recorded TiC fixture.
- **Steps:** Add index-file parsing → per-payer in-network URL discovery; feed the
  existing ingest; store at **plan granularity** where the file exposes plan ids.
- **Gate:** Test ingests a recorded TiC index → correct NPIs per payer/plan; payer
  flips to verified and supersedes its estimate.
- **Commit:** `C2: auto-discover TiC in-network files from public index, ingest at scale`

#### C3 — Free scheduled ingestion + freshness SLOs
- **Goal:** Zero manual data steps; data never silently goes stale.
- **Files:** `.github/workflows/ingest.yml` (cron), `app/main.py` (secured admin
  ingest trigger or scheduled container), `app/db.py` (store `last_ingest` per source),
  `app/main.py` `/healthz`.
- **Steps:** GitHub Actions cron runs Medicare (quarterly) + TiC (monthly) against the
  deployed DB via a token-secured endpoint; `/healthz` reports data ages and fails SLO
  when stale (Medicare > 1 quarter, any payer > N days).
- **Gate:** Cron runs end-to-end on free tier; `/healthz` exposes ages and flips
  unhealthy when stale (tested with a clock shim).
- **Commit:** `C3: schedule ingestion via free CI cron; enforce data-age SLOs`

#### C4 — Coverage dashboard + NPPES result caching
- **Goal:** Make verified coverage visible; stop hammering NPPES.
- **Files:** `app/metrics.py`/new `app/coverage.py`, `app/nppes.py` (add short-TTL
  cache via the `CacheBackend` from B2), `/metrics`.
- **Steps:** Compute verified-provider % by state per ingest; cache NPPES search
  results with a short TTL keyed on all params.
- **Gate:** Coverage report regenerates each ingest; identical repeat NPPES search
  makes zero live calls (asserted via metric).
- **Commit:** `C4: verified-coverage-by-state report + short-TTL NPPES cache`

---

### PHASE D — Experience & hardening (#5, #6, #7, #4)

#### D1 — Frontend performance + PWA (#5)
- **Files:** `src/ui.js` (virtualization), `manifest.webmanifest`, `sw.js`, `build.mjs`.
- **Steps:** Virtualize the results list; add manifest + service worker (offline shell,
  politely cached tiles); eliminate CLS.
- **Gate:** Lighthouse ≥98 all four; 1,000-result list at 60fps; installable + offline
  cached search works.
- **Commit:** `D1: virtualized results, installable PWA, offline shell`

#### D2 — Accessibility to WCAG 2.2 AA, verified (#6)
- **Files:** `tests-e2e/a11y.spec.js` (axe across all states), `carefind.html`/`src/`,
  `docs/a11y-walkthrough.md`.
- **Steps:** axe in CI with zero violations on every view+state; make the map
  keyboard-navigable or provide an equivalent accessible path; verify all color tokens
  ≥4.5:1; record an NVDA + VoiceOver walkthrough.
- **Gate:** axe 0 violations all states; WCAG 2.2 AA checklist 100%; keyboard-only
  golden journey; committed SR walkthrough.
- **Commit:** `D2: WCAG 2.2 AA — axe-clean all states, accessible map, SR walkthrough`

#### D3 — Strict CSP, full headers, threat model, privacy (#7)
- **Files:** `carefind.html` (CSP), `Caddyfile`, `app/main.py` (`/metrics` auth, input
  caps, log scrubbing), `docs/threat-model.md`.
- **Steps:** Remove `'unsafe-inline'` from `script-src` (enabled by B1's bundle +
  nonce); add HSTS/X-Content-Type-Options/Referrer-Policy/Permissions-Policy; protect
  `/metrics`; cap input param lengths; ensure search terms/IPs aren't persisted; write
  the threat model.
- **Gate:** securityheaders.com A+; CSP has no `unsafe-inline`; test proves no PII in
  persistent logs; threat model committed.
- **Commit:** `D3: strict CSP, full security headers, threat model, PII-free logs`

#### D4 — Backend resilience + multi-worker correctness (#4)
- **Files:** `app/main.py`, `app/interfaces.py` impls (shared rate-limit/metrics via
  free Redis tier behind the B2 interface), `app/nppes.py`/`app/insurance.py` (circuit
  breakers), optional Postgres `Datastore`, `tests/`, `loadtest/` (k6/locust).
- **Steps:** Provide a shared `RateLimiter`/metrics backend option (free Redis tier);
  add circuit breakers on NPPES/FHIR; add a Postgres datastore impl (free tier) for
  multi-worker; `/readyz`; graceful shutdown; free local load + chaos tests.
- **Gate:** correct with 4 workers; load test meets p95; chaos test leaks zero 5xx;
  all upstream calls bounded+retried+circuit-broken.
- **Commit:** `D4: circuit breakers, shared rate-limit/metrics, multi-worker datastore`

---

### PHASE E — Proof (#8, #10, #1) + final re-audit

#### E1 — Testing to the 10 bar (#8)
- **Files:** `tests-js/` (UI module coverage), `tests/`, contract cassettes
  (`tests/cassettes/`), mutation config (mutmut/Stryker), `playwright.config.js`
  (3 engines), `.github/workflows/nightly.yml`.
- **Steps:** Raise both suites to ≥90% line+branch (gated); contract-test NPPES/FHIR/
  TiC adapters against recorded real responses; add mutation testing with a score
  gate; run e2e on chromium+firefox+webkit; nightly live-integration job against real
  public endpoints.
- **Gate:** ≥90% line+branch both; mutation score ≥ target; e2e green on 3 engines;
  nightly live-integration green.
- **Commit:** `E1: 90% branch coverage, contract cassettes, mutation + cross-browser + nightly live`

#### E2 — Documentation, runbooks, provenance, OpenAPI (#10)
- **Files:** `docs/architecture.md`, `docs/adr/*.md`, `docs/runbook.md`,
  `docs/provenance.md` (auto-generated by `verify_payers`), `Readme.md`, CI link-check.
- **Steps:** Architecture overview + diagram; ADRs for the load-bearing decisions
  (two-tier model, source abstraction, $0 infra, SQLite↔Postgres seam); operator
  runbook (deploy/ingest/backup/restore/incident); publish FastAPI OpenAPI with
  examples; auto-current provenance ledger; CI link-checks docs and executes doc
  examples.
- **Gate:** validated zero→running in <15 min from docs alone; docs link-checked in
  CI; OpenAPI published; provenance ledger current.
- **Commit:** `E2: architecture docs, ADRs, runbook, OpenAPI, auto provenance ledger`

#### E3 — Backups, restore drill, monitoring (#9 completion)
- **Files:** `docs/runbook.md`, deploy config, `.github/workflows/` (dead-man's-switch
  ping to Healthchecks.io), UptimeRobot setup notes.
- **Steps:** SQLite → Litestream to a free object store (e.g., Cloudflare R2 free
  tier) or scheduled committed dump; Postgres → provider free PITR; document + test a
  restore; Healthchecks.io on the ingest cron; UptimeRobot on `/healthz`.
- **Gate:** restore-from-backup tested + documented; dead-man's-switch fires on a
  stalled ingest.
- **Commit:** `E3: free backups + tested restore + uptime/ingest dead-man's-switch`

#### E4 — Product proof (#1) + FINAL re-audit
- **Files:** `tests-e2e/golden_journey.spec.js`, `docs/competitive-analysis.md`,
  `docs/audit.md` (the re-audit).
- **Steps:** Implement the golden-journey e2e; write the head-to-head comparison;
  then **re-run every gate in §2** and record the result in `docs/audit.md`.
- **Gate:** **All ten gates in §2 pass.** That passing re-audit *is* the 10/10.
- **Commit:** `E4: golden-journey e2e, competitive analysis, final 10/10 re-audit`

---

## 4. Per-task verification protocol

For every task, before opening the PR:
1. `pytest -q` green; relevant JS suite green; new tests cover new branches.
2. All CI gates (lint, types, scanners, coverage) green locally where runnable.
3. If the change is observable, run the app
   (`uvicorn app.main:app --reload --port 8000`, open `http://localhost:8000`) and
   exercise the affected path; capture proof.
4. Confirm the **trust invariant** (§1.1) is intact.
5. Update affected docs (README honesty table, provenance ledger) in the same PR.

## 5. Definition of Done (whole program)

The program is complete when `docs/audit.md` shows **every gate in §2 green**, the
nightly live-integration job is passing, and a fresh clone reaches "deployed +
ingesting on a free tier" in under 15 minutes following only the docs — all at **$0**.

## 6. Honest ceiling (read once)

Nine categories reach a literal 10 at $0 and are verifiable locally. **Category 2's
10 is defined as maximal verified coverage from free public data + zero overclaim +
full provenance** — not nationwide, plan-level, real-time eligibility, which does not
exist for free. The plan pushes coverage to the public-data ceiling (mandated FHIR
Plan-Net + public TiC) and never presents anything beyond what a real source confirms.
That honesty *is* the 10 for this category.
