# Deploy CareFind for $0 (Oracle Cloud Always Free + DuckDNS)

This is the concrete, free deployment path. It runs the **shipped** `docker-compose.yml`
+ `Caddyfile` unchanged, with a persistent disk for the 2.5M-row Medicare index and a
real Let's Encrypt TLS certificate — all at $0/month.

## Why this stack (first principles)

CareFind's one hard hosting constraint is the **Medicare SQLite index (~2.5M rows) that
must persist across restarts**. The usual "free tier" PaaS options (Render/Koyeb/HF
Spaces free) have **ephemeral disks and sleep on idle** — every cold start wipes the DB
and forces a full re-ingest of the CMS quarterly file, so searches show 0 Medicare until
it finishes. That makes them unusable for this app. A small always-on VM with a real disk
is the only $0 option that runs the shipped artifacts as-is.

- **Host — Oracle Cloud "Always Free" VM.** A genuinely free-forever VM with persistent
  block storage (1 GB RAM AMD micro is plenty; the ARM Ampere shape is generous if
  available). A credit card is required **for identity verification only** — Always Free
  shapes never bill. This is the only owner step that needs a human.
- **DNS — DuckDNS** (or any free dynamic-DNS). A free `*.duckdns.org` subdomain → an A
  record at the VM's public IP, so Caddy fetches a real Let's Encrypt cert on first boot.
  No card, no domain purchase.

> Cardless fallback (inferior, documented for honesty): if you cannot do the card
> verification, a free PaaS will run the *web app*, but you must accept that the Medicare
> index won't persist — you'd re-ingest on every cold start (set `CAREFIND_MEDICARE_INGEST_URL`
> and trigger `/admin/ingest` after each wake). Don't choose this unless Oracle is truly
> impossible; the data UX is poor.

---

## Owner steps (the only parts that need a human)

1. **Create the Oracle Cloud account** → https://www.oracle.com/cloud/free/ (card for ID
   verification; pick an Always Free region).
2. **Launch an Always Free VM**: Ubuntu 22.04/24.04, an Always-Free-eligible shape
   (`VM.Standard.E2.1.Micro`, or `VM.Standard.A1.Flex` 1 OCPU/6 GB if offered). Add your
   SSH key. Note the **public IP**.
3. **Open ports 80 and 443**: in the VCN's default Security List add ingress rules for TCP
   80 and 443 from `0.0.0.0/0`. (Oracle's default only opens 22.)
4. **Get a free subdomain**: sign in at https://www.duckdns.org with GitHub/Google, create
   e.g. `carefind`, copy the **token**. Point it at the VM IP (the setup script below does
   this for you, or set the IP in the DuckDNS UI).

Then hand me the **public IP**, the **subdomain** (`carefind.duckdns.org`), and the
**DuckDNS token** and I'll finalize the config — or just run the steps below yourself.

---

## Bring it up (run on the VM)

```bash
# 0) Docker (Ubuntu)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# 1) Get the code
git clone <your-fork-url> carefind && cd carefind

# 2) One-shot configure + launch (see deploy/setup.sh)
DUCKDNS_NAME=carefind \
DUCKDNS_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
CONTACT_EMAIL=you@example.com \
ADMIN_TOKEN="$(openssl rand -hex 32)" \
bash deploy/setup.sh
```

`deploy/setup.sh` (idempotent) does the mechanical work:
- points the DuckDNS A record at this VM's public IP;
- writes `.env` (real `ALLOWED_ORIGINS`, `CAREFIND_UA`, `CAREFIND_ADMIN_TOKEN`, proxy trust);
- runs `configure_frontend.py https://<subdomain>.duckdns.org` (apiBase + CSP);
- sets the Caddy site address to your subdomain;
- `docker compose up -d --build`.

## Seed the data (once; then the cron keeps it fresh)

```bash
# Medicare quarterly enrollment file (real, nationwide ~2.5M NPIs). Find the current
# quarter's CSV URL at data.cms.gov (Medicare Fee-For-Service Public Provider Enrollment).
docker compose exec api python -m app.ingest_medicare "<CMS-quarterly-csv-url>"
docker compose exec api python -m app.verify_payers   # validate Plan-Net + write the ledger
```

For unattended freshness, set the GitHub Actions secrets the
[ingest cron](../.github/workflows/ingest.yml) needs: `CAREFIND_URL=https://<subdomain>.duckdns.org`,
`CAREFIND_ADMIN_TOKEN` (same value as above), and `HEALTHCHECK_PING_URL` (free
Healthchecks.io dead-man's switch).

## Verify it's live

```bash
curl -s https://<subdomain>.duckdns.org/readyz          # -> 200
curl -s https://<subdomain>.duckdns.org/healthz         # -> 200 (503 = data stale, re-ingest)
curl -s https://<subdomain>.duckdns.org/api/insurance/plans | python3 -c \
  "import sys,json;print(sorted(p['id'] for p in json.load(sys.stdin)['plans'] if p['confidence']=='verified'))"
# expect: ['cigna','humana','medicare','priority_partners','unitedhealthcare']
```

Point a free uptime monitor (UptimeRobot) at `/healthz` — it returns 503 on stale data,
which is the signal that an ingest was missed.
