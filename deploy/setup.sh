#!/usr/bin/env bash
# Configure + launch InNetwork on a fresh VM (Oracle Cloud Always Free + DuckDNS path).
# See docs/deploy.md. Idempotent: safe to re-run. Run from the repo root on the VM.
#
#   DUCKDNS_NAME=innetwork \
#   DUCKDNS_TOKEN=xxxx \
#   CONTACT_EMAIL=you@example.com \
#   ADMIN_TOKEN="$(openssl rand -hex 32)" \
#   [CLAIM_EMAIL=providers@example.com] \
#   bash deploy/setup.sh
set -euo pipefail

: "${DUCKDNS_NAME:?set DUCKDNS_NAME (e.g. innetwork for innetwork.duckdns.org)}"
: "${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN (from duckdns.org)}"
: "${CONTACT_EMAIL:?set CONTACT_EMAIL (real email; identifies you to NPPES/Nominatim)}"
: "${ADMIN_TOKEN:?set ADMIN_TOKEN (e.g. \$(openssl rand -hex 32))}"
CLAIM_EMAIL="${CLAIM_EMAIL:-}"

DOMAIN="${DUCKDNS_NAME}.duckdns.org"
ORIGIN="https://${DOMAIN}"
cd "$(dirname "$0")/.."   # repo root

echo "==> 1/5 Point DuckDNS ${DOMAIN} at this VM's public IP"
# Empty ip= lets DuckDNS use the request's source IP — the VM's public IP.
resp=$(curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_NAME}&token=${DUCKDNS_TOKEN}&ip=")
[ "$resp" = "OK" ] || { echo "DuckDNS update failed: '$resp' (check name/token)"; exit 1; }
echo "    DuckDNS: OK"

echo "==> 2/5 Write .env"
cat > .env <<EOF
ALLOWED_ORIGINS=${ORIGIN}
GEOCODE_USE_CENSUS=true
INNETWORK_UA=InNetwork/3.1 (+${ORIGIN}; contact: ${CONTACT_EMAIL})
RATE_LIMIT_MAX=60
RATE_LIMIT_WINDOW=60
INNETWORK_TRUST_PROXY=true
GEOCODE_MIN_INTERVAL=1.0
INNETWORK_PAYERS=payers.json
INNETWORK_ADMIN_TOKEN=${ADMIN_TOKEN}
EOF
echo "    wrote .env (ALLOWED_ORIGINS=${ORIGIN})"

echo "==> 3/5 Point the frontend (apiBase + CSP) at ${ORIGIN}"
# Page and API are same-origin behind Caddy, so apiBase == the site origin.
if [ -n "$CLAIM_EMAIL" ]; then
  python3 configure_frontend.py "$ORIGIN" --claim-email "$CLAIM_EMAIL"
else
  python3 configure_frontend.py "$ORIGIN"   # claim affordances stay hidden until set
fi

echo "==> 4/5 Set the Caddy site address to ${DOMAIN}"
# The shipped Caddyfile ships with the api.yourdomain.com placeholder; swap it for the
# real domain so Caddy requests a Let's Encrypt cert for it. (CSP connect-src is 'self' —
# same-origin — so only the site label needs changing.)
sed -i "s/api\.yourdomain\.com/${DOMAIN}/g" Caddyfile

echo "==> 5/5 Build + launch (uvicorn 1 worker behind Caddy)"
docker compose up -d --build

cat <<EOF

Done. Caddy will fetch a Let's Encrypt cert for ${DOMAIN} within ~30s (needs ports 80/443
open in the Oracle VCN security list, and DNS propagated — usually instant for DuckDNS).

Next — seed the data (once):
  docker compose exec api python -m app.ingest_medicare "<CMS-quarterly-csv-url>"
  docker compose exec api python -m app.verify_payers

Verify:
  curl -s ${ORIGIN}/readyz
  curl -s ${ORIGIN}/healthz
EOF
