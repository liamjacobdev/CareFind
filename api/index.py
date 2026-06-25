"""Vercel serverless entry point — CareFind on Vercel's Python runtime ($0, cardless).

Vercel functions have a read-only bundle filesystem and an ephemeral, writable /tmp.
CareFind's data path (Medicare/TiC lookups) is read-only at serve time, so we ship a
gzipped SQLite seed in the deployment and inflate it to /tmp once per cold start; the
handful of cache writes (FHIR/geocode) then land in that writable /tmp copy. See
docs/deploy.md.

Everything below runs at import (cold start), BEFORE app.config reads the environment:
  • seed /tmp/carefind.db from the committed carefind.db.gz;
  • pin serverless-safe settings (keyless Census geocoder; per-process rate limiter off —
    it's meaningless across isolated function instances);
  • build the registry explicitly, because Vercel's ASGI runtime does not reliably fire
    FastAPI's lifespan startup (where db.init_db()/registry.build() normally run).
"""
import gzip
import os
import pathlib
import shutil

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SEED = _ROOT / "carefind.db.gz"
_RUNTIME = "/tmp/carefind.db"

# 1) Inflate the read-only seed into writable /tmp once per cold start.
if not os.path.exists(_RUNTIME) and _SEED.exists():
    with gzip.open(_SEED, "rb") as src, open(_RUNTIME, "wb") as dst:
        shutil.copyfileobj(src, dst)

# 2) Serverless-safe defaults (set BEFORE importing app.config, which reads os.environ).
os.environ["CAREFIND_DB"] = _RUNTIME
os.environ.setdefault("GEOCODE_USE_CENSUS", "true")   # keyless, no rate limit
os.environ.setdefault("RATE_LIMIT_MAX", "0")          # per-process limiter is moot here
os.environ.setdefault(
    "CAREFIND_UA",
    "CareFind/3.1 (+https://github.com/; set CAREFIND_UA to your contact email)",
)

# 3) Import the app and run the init that lifespan would have (idempotent).
from app import db                       # noqa: E402
from app.insurance import registry       # noqa: E402
from app.main import app                 # noqa: E402  (the ASGI app Vercel serves)

db.init_db()
registry.build()
