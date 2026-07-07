"""Vercel serverless entry point — InNetwork on Vercel's Python runtime ($0, cardless).

Vercel functions have a read-only bundle filesystem and an ephemeral, writable /tmp. Since
the rebuild, InNetwork's verified insurance is a set of Roaring membership bitmaps
(payers/*.roaring) that are **mmap'd read-only straight from the deployment bundle** — so
there is no gzip-SQLite-into-/tmp inflate on cold start anymore. The only writable state is
the ephemeral cache DB (live-FHIR results + geocodes) in /tmp, created empty per cold
start. See docs/deploy.md.

Everything below runs at import (cold start), BEFORE app.config reads the environment:
  • point the runtime SQLite (caches only) at writable /tmp;
  • point the membership store at the bundled payers/ dir (absolute, CWD-independent);
  • pin serverless-safe settings (keyless Census geocoder; per-process rate limiter off);
  • build the registry explicitly, because Vercel's ASGI runtime does not reliably fire
    FastAPI's lifespan startup (where db.init_db()/registry.build() normally run).
"""
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 1) Runtime SQLite holds ONLY ephemeral caches (FHIR/geocode) now — Medicare and every
#    harvested payer are served from the read-only bitmaps. Create it fresh in /tmp; no
#    seed to inflate.
os.environ["INNETWORK_DB"] = "/tmp/innetwork.db"
# 2) Verified membership bitmaps ship in the bundle under payers/; resolve it absolutely so
#    it's found regardless of the function's working directory.
os.environ.setdefault("INNETWORK_MEMBERSHIP_DIR", str(_ROOT / "payers"))
# 3) Serverless-safe defaults (set BEFORE importing app.config, which reads os.environ).
os.environ.setdefault("GEOCODE_USE_CENSUS", "true")   # keyless, no rate limit
os.environ.setdefault("RATE_LIMIT_MAX", "0")          # per-process limiter is moot here
# One process serves page + API → point the page at its own origin automatically, so the
# first deploy works with no configure_frontend step.
os.environ.setdefault("INNETWORK_SAME_ORIGIN", "true")
os.environ.setdefault(
    "INNETWORK_UA",
    "InNetwork/3.1 (+https://github.com/; set INNETWORK_UA to your contact email)",
)

# 4) Import the app and run the init that lifespan would have (idempotent).
from app import db                       # noqa: E402
from app.insurance import registry       # noqa: E402
from app.main import app                 # noqa: E402  (the ASGI app Vercel serves)

db.init_db()
registry.build()
