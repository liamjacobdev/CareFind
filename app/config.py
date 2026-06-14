"""Runtime configuration, sourced from environment variables with safe defaults.

Nothing here is secret-by-default; the only values you must set for production are
ALLOWED_ORIGINS (lock CORS to your frontend) and, if you use commercial payers,
the entries in payers.json.
"""
import json
import os
from pathlib import Path

# The shipped default User-Agent. Nominatim's free usage policy rejects placeholder/
# templated agents (HTTP 403), so this only matters for the optional Nominatim
# fallback — geocoding works out of the box via the Census source regardless.
_DEFAULT_UA = "CareFind/3.1 self-hosted (single-user; set CAREFIND_UA with your email)"


class Settings:
    def __init__(self):
        # SQLite file holding the Medicare index + geocode cache. Keep it on a
        # persistent volume in production so both survive restarts.
        self.db_path = os.environ.get("CAREFIND_DB", "./carefind.db")

        # Comma-separated list of origins allowed to call the API. Empty -> "*"
        # (fine for local dev; set this to your real frontend origin in prod).
        origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
        self.allowed_origins = [o.strip() for o in origins.split(",") if o.strip()]

        # Nominatim and NPPES both ask callers to identify themselves. Put a real
        # contact in CAREFIND_UA before pointing this at the live services.
        # Nominatim/NPPES ask callers to identify themselves with a real contact.
        # Set CAREFIND_UA to your own email before any heavy use — the public
        # OpenStreetMap geocoder blocks placeholder/templated user-agents (HTTP 403).
        self.contact_ua = os.environ.get("CAREFIND_UA", _DEFAULT_UA)
        # True when CAREFIND_UA is still the shipped placeholder — i.e. the Nominatim
        # fallback won't work (403). Geocoding still works via Census; used only to
        # decide whether to warn at startup.
        self.ua_is_placeholder = self.contact_ua == _DEFAULT_UA

        # Primary geocoder: the free, keyless, US-only Census Geocoder. Set
        # GEOCODE_USE_CENSUS=false to force the Nominatim-only path (needs CAREFIND_UA).
        self.geocode_use_census = os.environ.get(
            "GEOCODE_USE_CENSUS", "true"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.census_base = os.environ.get(
            "CENSUS_GEOCODER_BASE", "https://geocoding.geo.census.gov"
        ).rstrip("/")

        self.nominatim_base = os.environ.get(
            "NOMINATIM_BASE", "https://nominatim.openstreetmap.org"
        ).rstrip("/")
        self.nppes_base = os.environ.get(
            "NPPES_BASE", "https://npiregistry.cms.hhs.gov/api/"
        )

        # Where to find configured commercial payers (see payers.example.json).
        self.payers_file = os.environ.get("CAREFIND_PAYERS", "payers.json")

        # Hard ceiling on a single ingest download (TiC / Medicare). Remote files
        # are streamed and aborted the moment they exceed this, so a hostile or
        # mistyped URL can't OOM the box by being read fully into memory. Default
        # 2 GiB — well above any real single-payer file, far below "exhaust RAM".
        self.ingest_max_bytes = int(os.environ.get("CAREFIND_INGEST_MAX_BYTES", str(2 * 1024**3)))

        # Polite minimum seconds between live Nominatim requests (their usage
        # policy is max 1 req/sec). The cache means we rarely hit this.
        self.geocode_min_interval = float(os.environ.get("GEOCODE_MIN_INTERVAL", "1.0"))

        # Per-client rate limit on /api/* (requests per window, window seconds).
        # Protects the upstream registries from abuse via this open proxy. 0 = off.
        self.rate_limit_max = int(os.environ.get("RATE_LIMIT_MAX", "60"))
        self.rate_limit_window = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

        # When deployed behind a trusted reverse proxy (the documented Caddy setup),
        # the real client IP arrives in X-Forwarded-For; without this the limiter sees
        # only the proxy's IP and collapses into one global bucket. Enable ONLY behind
        # a proxy you control — otherwise a client could spoof the header to dodge the
        # limit. The provided docker-compose sets this for the Caddy front-end.
        self.trust_proxy = os.environ.get("CAREFIND_TRUST_PROXY", "false").strip().lower() in (
            "1", "true", "yes", "on",
        )

    def load_payers(self) -> list:
        """Read payers.json -> list of payer config dicts. Missing file is fine."""
        path = Path(self.payers_file)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict):
            return data.get("payers", []) or []
        return data if isinstance(data, list) else []


settings = Settings()
