# CareFind API

The real backend behind CareFind. It does four jobs:

1. **Proxies the official CMS NPPES registry** (`/api/npi`, `/api/providers/search`) — server-side, so the browser never depends on flaky public CORS proxies.
2. **Batches geocoding server-side** (`/api/geocode/batch`) with a persistent SQLite cache. Works **out of the box** via the free, keyless [US Census Geocoder](https://geocoding.geo.census.gov) (US-only — exactly this app's scope, with no rate limit); OpenStreetMap Nominatim is an optional fallback (set `CAREFIND_UA` to a real contact email to enable it — Nominatim rejects placeholder agents). One request from the browser geocodes a whole page of results.
3. **Resolves real insurance acceptance** (`/api/insurance/...`) — Medicare out of the box, commercial payer networks as you configure them. No fabricated data, ever.
4. **Orchestrates all of the above** in one call (`/api/providers/search`) so the frontend gets providers, insurance flags, and coordinates in a single round trip.

---

## The insurance data — what's real, and how

CareFind covers **many plans, grouped by category** (Medicare, Medicare Advantage, Medicaid, Commercial/Employer, ACA Marketplace, TRICARE, VA) **and** by named payer (UnitedHealthcare, Aetna, Cigna, Blue Cross Blue Shield, Humana, Kaiser, …). It never fabricates — instead every answer carries a **confidence tier**:

- **Verified** (green *Confirmed* badge): confirmed for *this* provider from a real source — the Medicare enrollment file, a payer's FHIR Plan-Net directory, or an ingested Transparency-in-Coverage file.
- **Estimated** (amber *Likely* badge): a major payer that operates in the provider's state, from the curated catalog in `app/catalog.py`. Shown as "likely — confirm with the provider," **never** as confirmed. A verified source always supersedes an estimate.

**Verified by default.** The filter defaults to **Verified only**: estimated payers are hidden from the filter, estimated badges never render, and search requires confirmed acceptance (`accepts_mode=verified`). The **Include estimated** toggle (`accepts_mode=any`) is the *only* way estimates surface, and even then they read "likely — confirm," never *Confirmed*. As a payer gets backed by a verified source (FHIR Plan-Net or a TiC ingest) it graduates to a green *Confirmed* filter automatically — no UI change, because the catalog `id` is the stable join key.

### Source 1 — Medicare (works once you ingest one file)
The CMS **Medicare Fee-For-Service Public Provider Enrollment** dataset lists every NPI approved to bill Medicare. It's free, national, and updated quarterly.

- Dataset: https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment
- Download the CSV, then ingest:

```bash
python -m app.ingest_medicare /path/to/enrollment.csv
# or stream from a URL:
python -m app.ingest_medicare "https://data.cms.gov/.../enrollment.csv"
```

Re-run quarterly to refresh. After ingest, "Medicare" appears as a verified filter and matching providers show a **Confirmed** badge.

### Source 2 — Commercial networks via FHIR Plan-Net (real, validated, auto-wired)
Under the CMS Interoperability rule (CMS-9115-F), Medicare Advantage, Medicaid, and CHIP payers must publish a **public, unauthenticated Provider Directory API** in FHIR R4 (Da Vinci PDEX Plan-Net). CareFind queries it by NPI to confirm network participation.

The **validated public endpoints** in `app/planet_registry.py` are wired as *Confirmed* filters **out of the box** — no config. `python -m app.verify_payers` live-checks each one and regenerates the provenance ledger ([docs/provenance.md](docs/provenance.md)). To add a payer that isn't in the registry (e.g. one needing a free API key), copy `payers.example.json` to `payers.json`; a payer returns **in-network / not-found / unknown** and CareFind never turns "unknown" into a yes.

**What "validated" requires (the trust gate).** It is *not* enough that `/PractitionerRole` returns a Bundle. The validator runs the exact per-NPI lookup the app performs and only wires an endpoint that answers it truthfully **both** ways:
- a **bogus** NPI must *not* resolve in-network — otherwise the directory ignores the NPI filter and would mark everyone in-network (a fabricated *yes*; e.g. Connecticut's Medicaid directory does this);
- a **real, listed** NPI must resolve in-network — otherwise per-NPI search returns nothing for everyone (a fabricated *no*; e.g. Premera and the reachable state-Medicaid directories do this).

**Validated public endpoints** (live-checked 2026-06-16; see [docs/provenance.md](docs/provenance.md) for the full, auto-generated ledger including the tracked-but-not-wired ones):

| Payer (scope) | catalog id | Base URL | Round-trip |
|---|---|---|---|
| Priority Partners — Johns Hopkins (MD Medicaid) | `priority_partners` | `https://api.jhhpfhir.com/r4/public-pp` | ✓ bogus→none, listed→in-network (Bundle 83,024) |
| Johns Hopkins Advantage MD (MD Medicare Advantage) | `advantage_md` | `https://api.jhhpfhir.com/r4/public-ma` | ✓ bogus→none, listed→in-network (Bundle 107,487) |

> **Honest finding (why only two).** National carriers (UnitedHealthcare, Aetna, Cigna,
> Humana) gate their Plan-Net behind developer registration. Many *public* directories —
> Premera, and the reachable State Medicaid directories from the
> [CMS SMA-Endpoint-Directory](https://github.com/CMSgov/SMA-Endpoint-Directory) — return a
> Bundle but **fail the per-NPI round-trip** (no network links and/or empty results for
> listed NPIs), so wiring them would fabricate answers. They stay **estimated**, never
> verified. The freely-validatable, NPI-usable public set is genuinely small today; the
> registry + `verify_payers` make growing it turnkey, and an endpoint graduates to
> *Confirmed* automatically the moment it passes — never by assertion.

### Source 3 — Transparency-in-Coverage (verified commercial, by ingest)
Every commercial plan must publish machine-readable in-network files. Ingest a payer's in-network NPIs and that payer becomes a **verified** filter — a *Confirmed* badge that supersedes its estimated catalog entry. The payer id must match a catalog entry (`app/catalog.py`), e.g. `aetna`, `cigna`, `unitedhealthcare`:

```bash
python -m app.ingest_tic aetna /path/to/aetna_npis.csv
# accepts a CSV/list of NPIs, or a TiC in-network .json / .json.gz
python -m app.ingest_tic cigna "https://payer.example/in-network.json.gz"
```

**Scheduled refresh (monthly).** For ongoing operation, list each payer's published
in-network URL once in `tic_sources.json` (copy `tic_sources.example.json`) and run
the job — it ingests every configured payer and reports which flipped to *verified*.
Re-running is **idempotent**, so it's safe on a monthly cron:

```bash
cp tic_sources.example.json tic_sources.json   # then fill in real per-payer URLs
python -m app.ingest_tic_job          # refresh all configured payers
python -m app.ingest_tic_job aetna    # refresh just one
```

Document each payer's source URL and the date you retrieved it here as you wire them:

| Payer | TiC in-network source URL | Retrieved |
|-------|---------------------------|-----------|
| _(add each payer's published machine-readable index URL as you configure it)_ | | |

> Honest scope note: there is **no single free API** for all commercial insurers. The **estimated** tier gives you broad, recognizable named-payer filters on day one (clearly labeled, never presented as confirmed); the **verified** tier grows as you wire FHIR Plan-Net endpoints and ingest Transparency-in-Coverage files. Medicare is verified and national out of the box.

---

## Run it locally

```bash
pip install -r requirements.txt
export CAREFIND_DB=./carefind.db
python -m app.ingest_medicare sample_medicare.csv   # tiny demo file included (optional)
uvicorn app.main:app --reload --port 8000
# Open http://localhost:8000  — the backend serves the web page AND proxies the
# registry server-side, so provider search works with no browser CORS limits.
# (http://localhost:8000/healthz for a status check.)
```

**Windows, one click:** double-click `start-carefind.bat`. It uses the bundled
`.venv`, starts the backend, and opens `http://localhost:8000` for you.

> Note: the provider search queries the **live** CMS NPPES registry through the
> backend — there's no multi-GB database to download. Opening `carefind.html`
> as a plain file (or via a static server like `python -m http.server`) can't
> reach the registry from the browser; run the backend instead.

## Deploy to a domain you own

Docker Compose with Caddy handles TLS automatically.

1. **Point DNS** at your server: an `A` record for `api.yourdomain.com` → your server's IP.
2. **Edit `Caddyfile`** — replace `api.yourdomain.com` with that subdomain.
3. **Create `.env`** from `.env.example`; set `ALLOWED_ORIGINS` to the domain your frontend is served from (e.g. `https://carefind.yourdomain.com`).
4. **Bring it up:**

```bash
cp payers.example.json payers.json   # optional: add commercial payers
docker compose up -d --build
# Caddy fetches a Let's Encrypt cert for your domain automatically.
```

5. **Ingest Medicare into the running container:**

```bash
docker compose exec api python -m app.ingest_medicare "https://data.cms.gov/.../enrollment.csv"
```

6. **Connect the frontend** in one step (sets both `API_BASE` and the CSP `connect-src`, which must agree):

```bash
python configure_frontend.py https://api.yourdomain.com
# or write a separate file: --out carefind.prod.html
```

   Host the result on `https://carefind.yourdomain.com` (any static host). The insurance filter appears automatically once the API reports available plans.

Any container host works (Fly.io, Railway, Render, a VPS). Keep the SQLite volume persistent so the geocode cache and Medicare index survive restarts.

> **Reverse-proxy trust (rate limiting).** Behind Caddy, the app would otherwise see every request as coming from Caddy's container IP, collapsing the per-client rate limiter into a single global bucket that can lock out all users. The provided setup fixes this: uvicorn runs with `--proxy-headers` and `docker-compose.yml` sets `CAREFIND_TRUST_PROXY=true`, so the limiter buckets on the real client via `X-Forwarded-For` (Caddy's `reverse_proxy` sets it by default). **Only enable `CAREFIND_TRUST_PROXY` when the API is reachable solely through a proxy you control** — the `api` service uses `expose` (not `ports`), so it is not directly reachable. If you front it with something other than Caddy, ensure that proxy sets/overwrites `X-Forwarded-For`; otherwise leave the flag off so a client can't spoof the header to dodge the limit.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Status, Medicare index size, available plans |
| GET | `/api/insurance/plans` | Filterable plans — flat list + grouped `categories`, each with a `confidence` |
| GET | `/api/insurance/{npi}?state=` | Coverage for one NPI (`state` resolves estimated-tier plans) |
| GET | `/api/npi` | Proxied NPPES search (raw results) |
| GET | `/api/providers/search` | NPPES + insurance flags + plan filter + radius + batch geocode |
| GET | `/api/geocode?q=` | One geocode (cached) |
| POST | `/api/geocode/batch` | Batch geocode `{items:[{key,q}]}` |
| GET | `/api/reverse?lat=&lon=` | Reverse geocode → postcode |

`/api/providers/search` params: `zip, city, state, npi, name, taxonomy, type, limit`, `radius` (miles; widens beyond the exact ZIP and distance-filters), `accepts` (comma-sep plan ids), `accepts_mode` (`verified` | `any`), `geocode` (bool). Each provider's `insurance` is `{plan_id: {value, confidence, source}}`.

All `/api/*` routes are **rate-limited per client** (`RATE_LIMIT_MAX`/`RATE_LIMIT_WINDOW`; behind a proxy set `CAREFIND_TRUST_PROXY=true` so the bucket is the real client IP — see the deploy note above) and **CORS** is locked to `ALLOWED_ORIGINS` (localhost-only if unset — never a blanket `*`).

## Tests
```bash
pip install -r requirements-dev.txt
pytest          # NPPES params, DB/indexes, insurance confidence model, geocoder chain, API end-to-end

npm install
npm run build   # esbuild: bundle src/ -> carefind.bundle.js (the page loads this)
npm test        # Vitest unit tests for carefind.logic.js (enforces coverage threshold)
npm run test:e2e   # Playwright smoke (run `npx playwright install chromium` once first)
```
The frontend is authored as ES modules under `src/` (`config.js` reads the injected
`window.CAREFIND_CONFIG`; `main.js` is the app) plus the pure, unit-tested
`carefind.logic.js`. `npm run build` bundles them into a single same-origin
`carefind.bundle.js` — so the deploy story stays "one HTML file + the bundle + a
backend", the page carries no inline business logic, and the build is reproducible
(CI rebuilds and fails on any diff). Edit `src/` and rebuild; never hand-edit the
bundle. `tests/fixtures/normalize_golden.json` is asserted by both Python and JS so
the `normalize()` ↔ `buildProviders()` contract can't drift. CI runs all suites on
every push.

## What was tested vs. what needs your network
Verified here with an automated suite: app boots, DB/ingest (Medicare + TiC), the insurance confidence model (verified vs estimated, regional gating, verified-supersedes-estimate, post-startup TiC ingest with no restart), FHIR `check_many` mapping (mocked), the geocoder source chain (Census primary, Nominatim fallback, SQLite cache — mocked), NPPES param building incl. radius widening, per-client rate limiting behind a proxy, the batch-geocode cap, the `normalize()` golden shape, and server-authoritative radius search (out-of-radius dropped, distance-sorted, closest survive truncation) end-to-end against a mocked registry. **Not** reachable from the build sandbox, so verify in your environment: live NPPES results, live geocoding, and each payer's FHIR endpoint. The code paths and error handling for those are in place.
## Contributing & License
Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, the
test layout, and the trust rules (verified vs. estimated is sacred; never ship what you
can't verify). CareFind is released under the [MIT License](LICENSE).
