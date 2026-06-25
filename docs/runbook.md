# CareFind — operator runbook

Deploy, ingest, back up, restore, and respond to incidents. Everything here runs on
**free tiers**.

## Zero → running (target: < 15 min)

```bash
git clone <repo> && cd carefind

# Backend
python -m venv .venv && . .venv/Scripts/activate     # or .venv/bin/activate
pip install -r requirements.txt
python -m app.ingest_medicare sample_medicare.csv    # seed Medicare (or a real CMS CSV)
uvicorn app.main:app --port 8000                     # open http://localhost:8000
```

Verified FHIR Plan-Net endpoints (UnitedHealthcare, Cigna, Humana, Priority Partners) are
wired out of the box. The frontend is prebuilt (`carefind.bundle.js` is committed); to rebuild after a
`src/` change: `npm install && npm run build`.

**Production** (TLS + headers + real API origin):
```bash
python configure_frontend.py https://api.yourdomain.com --claim-email you@real.com
# rewrites carefind.config.js (apiBase/claimEmail) + the HTML CSP connect-src
docker compose up -d        # uvicorn (4 workers) behind Caddy (auto Let's Encrypt)
```
Set env: `ALLOWED_ORIGINS`, `CAREFIND_TRUST_PROXY=true`, `CAREFIND_ADMIN_TOKEN`,
`CAREFIND_UA=you@email`, and (for the cron) `CAREFIND_MEDICARE_INGEST_URL`.

## Ingestion (no manual data steps)
- **Automated:** the [scheduled-ingest cron](../.github/workflows/ingest.yml) POSTs the
  token-secured `/admin/ingest` — TiC monthly, Medicare quarterly. Set repo secrets
  `CAREFIND_URL` + `CAREFIND_ADMIN_TOKEN` (+ `HEALTHCHECK_PING_URL` for the dead-man's switch).
- **Manual:** `python -m app.ingest_tic <payer> <toc-or-file-url>` (auto-discovers a TiC
  index) · `python -m app.ingest_medicare <csv-or-url>` · `python -m app.verify_payers`
  (re-validate Plan-Net endpoints + regenerate `docs/provenance.md`).
- **Freshness:** `GET /healthz` reports per-source ages and returns **503** when a source
  is stale (Medicare > 100d, payers > 35d). Point an uptime monitor (UptimeRobot, free) at
  it; the ingest cron pings Healthchecks.io so a *missed* run alerts too.

## Backups & restore (tested)
SQLite at `CAREFIND_DB` holds the Medicare/TiC indexes + caches. All of it is
re-derivable from the public sources, so backup is cheap insurance, not a lifeline:
take a **scheduled dump** — `sqlite3 $CAREFIND_DB ".backup backup.db"` on a cron (or
call `app.db.backup()`) — and push it to any object store.

**Restore drill** (run quarterly — the indexes are re-ingestable, so the real risk is
config, not data loss):
```bash
sqlite3 carefind.db ".backup /tmp/restore-test.db"   # take a backup
CAREFIND_DB=/tmp/restore-test.db python -c "from app import db; print('medicare', db.medicare_count())"
```
A restored DB that reports the expected counts is a verified restore. Worst case, re-run
the ingests — all data is re-derivable from the public sources.

## Incident response
| Symptom | Check | Action |
|---|---|---|
| Searches 502 | `/metrics` `upstream_errors`, logs | NPPES likely down; requests time out and degrade. Confirm at npiregistry.cms.hhs.gov. |
| `/healthz` 503 | `data_freshness.stale` | An ingest stalled — re-run it (`/admin/ingest` or the CLI); check the cron + Healthchecks.io. |
| `/readyz` 503 | datastore reachable? | The worker can't reach SQLite/Postgres; the LB pulls it. Check the volume/connection. |
| A "Confirmed" looks wrong | `docs/provenance.md`, nightly job | Re-run `verify_payers`; if a payer endpoint regressed the round-trip, it's demoted automatically — never serve an unverifiable badge. |
| Map blank | browser console (CSP) | Tiles/Leaflet CDN blocked; the list still works (the accessible equivalent path). |

## Useful endpoints
`/healthz` (freshness/SLO) · `/readyz` (readiness) · `/metrics` (token-gated) ·
`/coverage` (verified-by-state) · `/docs` (Swagger) · `/openapi.json`.
