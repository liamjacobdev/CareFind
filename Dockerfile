FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + the static frontend and seed data.
COPY app/ ./app/
COPY innetwork.html innetwork.logic.js payers.example.json sample_medicare.csv ./

# The SQLite DB (Medicare index + geocode cache) lives on a mounted volume.
ENV INNETWORK_DB=/data/innetwork.db
VOLUME ["/data"]
EXPOSE 8000

# IMPORTANT: a single worker. The Nominatim politeness throttle and the in-process
# rate limiter are per-process; multiple workers would each get their own counter
# and could exceed Nominatim's 1 req/sec policy. Scale by fronting with a shared
# cache/limiter, not by adding workers here.
#
# --proxy-headers: behind the documented Caddy front-end, trust X-Forwarded-For so
# request.client.host is the real client (not the proxy's container IP). WHICH peers
# are trusted to set it is controlled by FORWARDED_ALLOW_IPS (uvicorn reads it from
# the env). We default it to 127.0.0.1 — the safe, deny-by-default value — rather
# than the old "*", which trusted a spoofed XFF from ANY source that could reach the
# port. docker-compose.yml overrides it with the *proxy network CIDR* so only the
# Caddy container is trusted. Pair with INNETWORK_TRUST_PROXY=true (set in compose).
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers"]
