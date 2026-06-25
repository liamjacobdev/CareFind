# Deploy CareFind for $0 — cardless, on Vercel

This is the **cardless, free** deployment path: CareFind runs as a single Vercel Python
serverless function that serves both the page and the API (same origin → no CORS), with
the 2.5M-row Medicare index shipped as a gzipped SQLite seed in the deployment. No credit
card, no database service, no separate frontend host.

## Why this works (first principles)

Vercel functions have a **read-only bundle filesystem** and an **ephemeral, writable
`/tmp`**. CareFind's data path — Medicare/TiC acceptance lookups — is **read-only at serve
time**, so we don't need a managed database at all:

- The seed `carefind.db.gz` (~27 MB) ships in the deployment; `api/index.py` inflates it
  to `/tmp/carefind.db` once per cold start (~98 MB, well under `/tmp`'s 512 MB).
- The few cache writes (FHIR/geocode) then land in that writable `/tmp` copy — ephemeral
  per instance, which is fine (they're best-effort and re-derivable).
- Geocoding uses the **keyless US Census geocoder** (no API key, no rate limit), so the
  per-process Nominatim throttle isn't needed.
- The per-process rate limiter is disabled (`RATE_LIMIT_MAX=0`) — it's meaningless across
  isolated serverless instances; rely on Vercel's platform protections.

Total function size (~27 MB seed + Python deps) sits comfortably under Vercel's ~250 MB
limit. Everything is verified locally via the cold-start simulation in the repo history.

## Deploy (cardless, ~5 minutes)

1. **Create a free Vercel account** at https://vercel.com (GitHub login; no card for the
   Hobby plan).
2. **Push this repo to GitHub** (the `carefind.db.gz` seed is committed, so the deploy is
   self-contained). On Vercel: **Add New → Project → import the repo**. Vercel detects
   `vercel.json` + `api/index.py` and the Python runtime automatically; click Deploy.
3. **Point the page at its own origin** so API calls resolve. Your production URL is
   `https://<project-name>.vercel.app`. Run once and redeploy (push):
   ```bash
   python configure_frontend.py https://<project-name>.vercel.app
   # rewrites carefind.config.js (apiBase) + the HTML CSP connect-src to your origin
   git commit -am "config: point frontend at the Vercel origin" && git push
   ```
4. (Optional) In the Vercel project's **Settings → Environment Variables**, set
   `CAREFIND_UA` to a real contact email (identifies you to NPPES; only required if you
   later enable the optional Nominatim geocoder fallback).

That's it. The same function serves `/`, the JS bundle, and `/api/*`.

## Verify it's live

```bash
curl -s https://<project-name>.vercel.app/readyz     # -> 200
curl -s https://<project-name>.vercel.app/healthz    # -> 200 (503 = data stale)
curl -s https://<project-name>.vercel.app/api/insurance/plans | python3 -c \
  "import sys,json;print(sorted(p['id'] for p in json.load(sys.stdin)['plans'] if p['confidence']=='verified'))"
# expect: ['cigna','humana','medicare','priority_partners','unitedhealthcare']
```

## Refreshing the data (quarterly Medicare, monthly TiC)

The seed is a point-in-time snapshot. To refresh, rebuild it locally and redeploy:

```bash
python -m app.ingest_medicare "<CMS-quarterly-csv-url>"   # updates ./carefind.db
python -m app.verify_payers                               # re-validate payers + ledger
gzip -9 -c carefind.db > carefind.db.gz                   # rebuild the seed
git commit -am "data: refresh Medicare seed" && git push  # Vercel auto-redeploys
```

This can be automated with a scheduled GitHub Action (ingest → gzip → commit), which then
triggers a Vercel deploy on push. Not wired by default to keep the repo's git history
free of large recurring binaries unless you want it.

## Known trade-offs (honest)

- **Cold starts** re-inflate the seed (~1 s) and rebuild the registry; first hit after idle
  is slower. Vercel keeps warm instances under traffic.
- **No durable write persistence** — the FHIR/geocode caches reset per instance/cold start.
  The valuable data (Medicare/TiC) is read-only and always present from the seed.
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
