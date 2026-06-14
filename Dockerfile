FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + the static frontend and seed data.
COPY app/ ./app/
COPY carefind.html payers.example.json sample_medicare.csv ./

# The SQLite DB (Medicare index + geocode cache) lives on a mounted volume.
ENV CAREFIND_DB=/data/carefind.db
VOLUME ["/data"]
EXPOSE 8000

# IMPORTANT: a single worker. The Nominatim politeness throttle and the in-process
# rate limiter are per-process; multiple workers would each get their own counter
# and could exceed Nominatim's 1 req/sec policy. Scale by fronting with a shared
# cache/limiter, not by adding workers here.
#
# --proxy-headers + --forwarded-allow-ips: behind the documented Caddy front-end,
# trust X-Forwarded-For so request.client.host is the real client (not the proxy's
# container IP). This is what makes the per-client rate limiter actually per-client.
# Pair with CAREFIND_TRUST_PROXY=true (set in docker-compose). Only safe because the
# only ingress is Caddy; do not expose this port directly to the internet.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
