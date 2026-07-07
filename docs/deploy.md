# Deploy InNetwork for $0 — cardless, on Vercel

This is the **cardless, free** deployment path: InNetwork runs as a single Vercel Python
serverless function that serves both the page and the API (same origin → no CORS), with
verified insurance shipped as compact **Roaring membership bitmaps** in the deployment. No
credit card, no database service, no separate frontend host.

## Why this works (first principles)

Vercel functions have a **read-only bundle filesystem** and an **ephemeral, writable
`/tmp`**. Verified insurance is a set-membership test against a bitmap, so it's **read-only
at serve time** — no managed database, and no cold-start inflate:

- Each verified payer's in-network NPI set is a `payers/<id>.roaring` bitmap (e.g. all
  2.5M Medicare NPIs in ~5 MB) plus a `payers/manifest.json`. They ship in the bundle and
  are **mmap'd read-only** straight from the read-only filesystem — nothing is inflated or
  copied to `/tmp`, so cold start has no seed step.
- The only writable state is an ephemeral cache DB (live-FHIR results + geocodes) created
  fresh in `/tmp/innetwork.db` per instance — best-effort and re-derivable.
- Geocoding uses the **keyless US Census geocoder** (no API key, no rate limit), so the
  per-process Nominatim throttle isn't needed.
- The per-process rate limiter is disabled (`RATE_LIMIT_MAX=0`) — it's meaningless across
  isolated serverless instances; rely on Vercel's platform protections.

Total function size (a few MB of bitmaps + Python deps) sits far under Vercel's ~250 MB
limit — headroom for 20-30 payers. Verified locally via the cold-start simulation.

## Deploy (cardless, ~5 minutes)

1. **Create a free Vercel account** at https://vercel.com (GitHub login; no card for the
   Hobby plan).
2. **Push this repo to GitHub** (the `payers/*.roaring` bitmaps are committed, so the deploy
   is self-contained). On Vercel: **Add New → Project → import the repo**. Vercel detects
   `vercel.json` + `api/index.py` and the Python runtime automatically; click Deploy.
   (`vercel.json`'s `includeFiles` bundles the `payers/` bitmaps into the function.)
3. **Point the page at your URL once.** After the first deploy you know your origin
   (`https://<project>.vercel.app`). Vercel serves `innetwork.config.js` as a *static* CDN
   asset (it bypasses the app's same-origin route), so bake the origin in and push:
   ```bash
   python configure_frontend.py https://<project>.vercel.app
   git commit -am "config: point frontend at the Vercel origin" && git push
   ```
   This is a one-time step; the redeploy serves the corrected config. (A separately-hosted
   frontend or the Docker self-host instead uses the app's automatic same-origin route via
   `INNETWORK_SAME_ORIGIN`; on Vercel the static-asset serving makes baking the simpler path.)
4. (Optional) In **Settings → Environment Variables**, set `INNETWORK_UA` to a real contact
   email (identifies you to NPPES; only needed if you later enable the Nominatim fallback).

The same function serves `/` and `/api/*`; `innetwork.bundle.js`/`config.js` are served as
static assets.

## Verify it's live

```bash
curl -s https://<project-name>.vercel.app/readyz     # -> 200
curl -s https://<project-name>.vercel.app/healthz    # -> 200 (503 = data stale)
curl -s https://<project-name>.vercel.app/api/insurance/plans | python3 -c \
  "import sys,json;print(sorted(p['id'] for p in json.load(sys.stdin)['plans'] if p['confidence']=='verified'))"
# expect: ['cigna','excellus','humana','medicare','priority_partners','unitedhealthcare']
```

## Refreshing the data (automated)

The bitmaps are point-in-time snapshots, each stamped with a `fetched_at` + freshness SLO
in `manifest.json` — `/healthz` flips to **503** when any served payer goes stale, the
dead-man's-switch an uptime monitor watches. Refresh a payer by re-harvesting its bitmap
and committing `payers/`, which triggers Vercel's GitHub integration to redeploy:

```bash
# Medicare — rebuild the bitmap from the current CMS enrollment file (auto-discovered):
python -c "from app.cms_catalog import latest_medicare_csv_url as u; print(u())"   # current CSV
python -m app.build_membership medicare "<that-url>"       # rebuild payers/medicare.roaring

# A FHIR-directory payer (Rail 1) — offline-harvest its whole network:
python -m app.harvest_fhir cigna                           # rebuild payers/cigna.roaring

# A TiC payer (Rail 2) — stream its in-network file(s):
python -m app.harvest_tic aetna "<tic-index-or-file-url>"  # rebuild payers/aetna.roaring

git commit -am "data: refresh payer bitmaps" && git push   # Vercel auto-redeploys
```

The heavy harvests (the national giants) are meant to run on a **public** GitHub Actions
matrix (unlimited free minutes on a public repo), sharded by facet, committing only the
bitmaps that changed. A failed harvest keeps the last-good bitmap and surfaces staleness —
it never ships an empty or partial "verified" set.

## Known trade-offs (honest)

- **Cold starts** just mmap the bitmaps + rebuild the registry (no seed inflate); first hit
  after idle is fast. Vercel keeps warm instances under traffic.
- **No durable write persistence** — the FHIR/geocode caches reset per instance/cold start.
  The valuable data (the membership bitmaps) is read-only and always present in the bundle.
- **First payer-filtered search** makes live FHIR calls per NPI (bounded, concurrent,
  cached for the instance's life); an unfiltered search makes zero and is fast.

---

## Alternative: self-host the container (needs a host, not cardless-PaaS)

For a long-running, fully-persistent deployment, the repo also ships `Dockerfile` +
`docker-compose.yml` + `Caddyfile` (uvicorn 1 worker behind Caddy with auto Let's
Encrypt, persistent volume for the SQLite index). Run `python configure_frontend.py
https://your.domain`, set `.env` (see `.env.example`), and `docker compose up -d`. This
needs a VM/host with a disk and a domain — see the runbook. It is the right choice if you
want durable write-persistence and no cold starts, but it isn't a cardless free PaaS.
