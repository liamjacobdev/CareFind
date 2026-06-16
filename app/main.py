"""CareFind backend — proxies NPPES + Nominatim, batches geocoding, and resolves
real insurance acceptance. Deploy behind TLS on your own domain (see README)."""
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import db, geocode, metrics, nppes
from .config import settings
from .insurance import registry
from .ratelimit import build_rate_limiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("carefind")

# Candidate-pool ceilings for radius searches: we widen the NPPES query (ZIP
# prefix) and over-fetch so distance-filtering has real recall before truncating
# to the caller's `limit`. A wider radius pulls a deeper pool.
_POOL_CEILING_SMALL = 100
_POOL_CEILING_LARGE = 200


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    registry.build()
    log.info("CareFind up: %d Medicare NPIs, %d insurance plans available",
             db.medicare_count(), len(registry.plans()))
    log.info("Geocoder: %s", geocode.active_geocoder())
    if not geocode.census_enabled() and settings.ua_is_placeholder:
        # Census is off AND the Nominatim UA is a placeholder → geocoding is broken.
        # (With Census on, a placeholder UA only disables the optional fallback.)
        log.warning(
            "Geocoding will fail: GEOCODE_USE_CENSUS is off and CAREFIND_UA is the "
            "placeholder, which Nominatim rejects (HTTP 403). Set CAREFIND_UA to your "
            "email, or enable the keyless US Census geocoder (GEOCODE_USE_CENSUS=true)."
        )
    yield


app = FastAPI(title="CareFind API", version="3.1", lifespan=lifespan)

# ── Per-client rate limiting ─────────────────────────────────────────────────
# Stops this open proxy from being driven to hammer NPPES/Nominatim. Behind the
# RateLimiter seam (app/interfaces.py): in-process per-worker by default; swap a
# shared backend (e.g. Redis) for a hard global cap across workers (D4) by setting
# CAREFIND_RATE_LIMITER. Registered before CORS below so CORS stays outermost and
# 429s carry CORS headers.
_limiter = build_rate_limiter()


def _client_ip(request: Request) -> str:
    """The IP the limiter buckets on. Behind a trusted proxy (settings.trust_proxy)
    the real client is the leftmost X-Forwarded-For entry; otherwise the direct peer.
    Without this, every request behind Caddy shares the proxy's IP and the per-client
    limiter degrades into a single global bucket that can lock out all users."""
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path.startswith("/api/"):
        ip = _client_ip(request)
        decision = _limiter.check(ip)
        if not decision.allowed:
            log.warning("rate limit hit for %s on %s", ip, request.url.path)
            return JSONResponse(
                {"detail": "Too many requests — slow down and try again shortly."},
                status_code=429, headers={"Retry-After": str(decision.retry_after)},
            )
    return await call_next(request)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Assign each request a short id (echoed as X-Request-ID and logged with the
    outcome, so a client's report is correlatable to a log line), and tally request
    metrics. Honors an inbound X-Request-ID from the trusted proxy for trace continuity."""
    rid = request.headers.get("x-request-id") if settings.trust_proxy else None
    rid = rid or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        metrics.incr("requests_total")
        metrics.incr("status:500")
        log.exception("unhandled error rid=%s %s %s", rid, request.method, request.url.path)
        raise
    dur_ms = (time.monotonic() - start) * 1000
    metrics.incr("requests_total")
    metrics.incr(f"status:{response.status_code}")
    response.headers["X-Request-ID"] = rid
    log.info("rid=%s %s %s -> %d (%.1fms)",
             rid, request.method, request.url.path, response.status_code, dur_ms)
    return response


# CORS: lock to configured origins in production. With none set we allow only
# localhost (dev convenience) — never a blanket '*' to arbitrary sites. Added
# last so it wraps the rate limiter and every response (incl. 429) gets headers.
_cors: dict[str, Any]
if settings.allowed_origins:
    _cors = {"allow_origins": settings.allowed_origins}
else:
    _cors = {"allow_origin_regex": r"https?://(localhost|127\.0\.0\.1)(:\d+)?"}
    log.warning("ALLOWED_ORIGINS not set — CORS limited to localhost only. "
                "Set ALLOWED_ORIGINS to your frontend origin in production.")
app.add_middleware(
    CORSMiddleware,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    **_cors,
)


# ── normalization: NPPES record -> clean provider dict the frontend renders ──
def _title(s: str | None) -> str:
    return " ".join(w.capitalize() for w in (s or "").split())


def normalize(r: dict[str, Any]) -> dict[str, Any]:
    npi = str(r.get("number", ""))
    b = r.get("basic", {}) or {}
    is_org = r.get("enumeration_type") == "NPI-2"
    if is_org:
        name = _title(b.get("organization_name") or b.get("name") or "Healthcare Organization")
    else:
        cred = f", {b['credential'].rstrip('.')}" if b.get("credential") else ""
        name = (_title(f"{b.get('first_name','')} {b.get('last_name','')}".strip()) + cred) or "Provider"
    addrs = r.get("addresses", []) or []
    loc = next((a for a in addrs if a.get("address_purpose") == "LOCATION"), addrs[0] if addrs else {})
    mail = next((a for a in addrs if a.get("address_purpose") == "MAILING"), None)

    def fmt(a: dict[str, Any] | None) -> str:
        if not a:
            return ""
        line = _title(" ".join(filter(None, [a.get("address_1"), a.get("address_2")])))
        return ", ".join(filter(None, [line, _title(a.get("city", "")), a.get("state", ""), (a.get("postal_code") or "")[:5]]))

    taxes = [
        {"desc": t.get("desc", ""), "code": t.get("code", ""), "primary": bool(t.get("primary")),
         "state": t.get("state", ""), "license": t.get("license", "")}
        for t in (r.get("taxonomies", []) or [])
    ]
    primary = next((t for t in taxes if t["primary"]), taxes[0] if taxes else {"desc": "Healthcare Provider"})
    return {
        "npi": npi, "name": name, "isOrg": is_org,
        "specialty": primary.get("desc", "Healthcare Provider"),
        "taxonomies": taxes,
        "address1": _title(" ".join(filter(None, [loc.get("address_1"), loc.get("address_2")]))),
        "city": _title(loc.get("city", "")), "stateAb": loc.get("state", ""),
        "postalCode": (loc.get("postal_code") or "")[:5],
        "fullAddress": fmt(loc), "mailingAddress": fmt(mail) if mail else "",
        "phone": loc.get("telephone_number", ""), "fax": loc.get("fax_number", ""),
        "gender": {"M": "Male", "F": "Female"}.get(b.get("gender") or "", ""),
        "soleProprietor": b.get("sole_proprietor", ""), "credential": b.get("credential", ""),
        "status": "Active" if b.get("status") == "A" else b.get("status", ""),
        "enumerationDate": b.get("enumeration_date", ""), "lastUpdated": b.get("last_updated", ""),
        "insurance": {},  # filled by the resolver
        "lat": None, "lng": None, "distance": None,
    }


# ── routes ──
# Serve the single-file frontend from the API itself, so running one command and
# opening http://localhost:8000 gives a fully working app: same origin (no CORS),
# with NPPES/geocoding proxied server-side. The file sits at the repo root next to
# app/ (and is COPYied next to app/ in the Docker image — same relative path).
_FRONTEND = Path(__file__).resolve().parent.parent / "carefind.html"


_FRONTEND_LOGIC = _FRONTEND.parent / "carefind.logic.js"
_FRONTEND_BUNDLE = _FRONTEND.parent / "carefind.bundle.js"


def _static_file(request: Request, path: Path, media_type: str, missing: str) -> Response:
    """Serve a static file with an ETag + short Cache-Control, and answer a matching
    If-None-Match with 304 so a repeat load isn't re-downloaded. The ETag is derived
    from the file's mtime+size, so editing the file invalidates caches automatically."""
    if not path.exists():
        raise HTTPException(404, missing)
    st = path.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers = {"Cache-Control": "public, max-age=300, must-revalidate", "ETag": etag}
    inm = request.headers.get("if-none-match", "")
    if etag in [t.strip() for t in inm.split(",") if t.strip()]:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


@app.get("/")
def index(request: Request) -> Response:
    return _static_file(request, _FRONTEND, "text/html",
                        "Frontend (carefind.html) not found next to the app package.")


@app.get("/carefind.bundle.js")
def frontend_bundle(request: Request) -> Response:
    # The page's interactive layer, bundled from src/ by `npm run build` (esbuild).
    return _static_file(request, _FRONTEND_BUNDLE, "application/javascript",
                        "carefind.bundle.js not found — run `npm run build`.")


@app.get("/carefind.logic.js")
def frontend_logic(request: Request) -> Response:
    # The shared pure logic module — a build input (bundled into carefind.bundle.js)
    # and the unit-tested source (Vitest). Still served for source transparency.
    return _static_file(request, _FRONTEND_LOGIC, "application/javascript",
                        "carefind.logic.js not found next to the app package.")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "medicare_npis": db.medicare_count(), "insurance_plans": registry.plans()}


@app.get("/metrics")
def metrics_endpoint() -> dict[str, Any]:
    """In-process operational metrics: request counts by status, geocode/FHIR cache
    hit rates, and upstream error count. Not under /api/, so it isn't rate-limited."""
    return metrics.snapshot()


@app.get("/api/insurance/plans")
def insurance_plans() -> dict[str, Any]:
    """Filterable plans — flat list plus grouped by coverage category. Each plan
    carries a `confidence` of 'verified' or 'estimated'."""
    return {"plans": registry.plans(), "categories": registry.categories()}


@app.get("/api/insurance/{npi}")
async def insurance_for(npi: str, state: str = "") -> dict[str, Any]:
    """Coverage for one NPI. Estimated-tier plans need a `state` to resolve;
    without it only verified answers appear."""
    return {"npi": npi, "insurance": await registry.check_all(npi, state)}


@app.get("/api/npi")
async def api_npi(
    zip: str = "", city: str = "", state: str = "", npi: str = "",
    name: str = "", taxonomy: str = "", type: str = "", limit: int = 25,
) -> dict[str, Any]:
    try:
        return {"results": await nppes.search(locals())}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        # Keep the user-facing 502 generic, but record what actually failed so a
        # broken upstream (NPPES down, network error) is visible in the logs. Use
        # e.__class__ (not type(e)) — `type` is shadowed by the query param above.
        metrics.incr("upstream_error")
        log.warning("NPPES search failed (zip=%s city=%s state=%s npi=%s name=%s): %s: %s",
                    zip, city, state, npi, name, e.__class__.__name__, e)
        raise HTTPException(502, "Could not reach the registry.") from e


@app.get("/api/geocode")
async def api_geocode(
    q: str = "", postalcode: str = "", city: str = "", state: str = ""
) -> dict[str, Any]:
    """Geocode a free-text `q`, or a structured {postalcode, city, state}. The
    frontend sends the structured form for the map center, so accept both rather
    than 422-ing on a missing `q`."""
    query = q.strip() or ", ".join(p for p in (city.strip(), state.strip(), postalcode.strip()) if p)
    return {"coords": await geocode.geocode_one(query)}


# Cap the batch so one large POST can't monopolize the single worker's geocoder.
_BATCH_MAX_ITEMS = 100


class BatchReq(BaseModel):
    items: list[dict[str, Any]]  # [{"key": str, "q": str}]


@app.post("/api/geocode/batch")
async def api_geocode_batch(req: BatchReq) -> dict[str, Any]:
    # Reject oversized batches: at the throttle rate an unbounded batch could block
    # every user's geocoding on this single worker for minutes (a DoS vector).
    if len(req.items) > _BATCH_MAX_ITEMS:
        raise HTTPException(413, f"Too many items — geocode at most {_BATCH_MAX_ITEMS} per request.")
    # Bounded budget too: 20s matches the search path; misses warm the cache over
    # repeat calls rather than holding the worker.
    return {"coords": await geocode.geocode_batch(req.items, budget_seconds=20)}


@app.get("/api/reverse")
async def api_reverse(lat: str, lon: str) -> dict[str, Any]:
    try:
        lat_f, lon_f = float(lat), float(lon)
    except ValueError:
        return {"postcode": ""}
    return {"postcode": await geocode.reverse(lat_f, lon_f)}


@app.get("/api/providers/search")
async def providers_search(
    zip: str = "", city: str = "", state: str = "", npi: str = "",
    name: str = "", taxonomy: str = "", type: str = "", limit: int = 25,
    radius: int = Query(0, description="Miles from the ZIP center to keep; 0 disables distance filtering"),
    accepts: str = Query("", description="Comma-separated plan ids the provider must accept, e.g. 'medicare,aetna'"),
    accepts_mode: str = Query("verified", description="'verified' = require confirmed acceptance; 'any' = also allow estimated matches"),
    geocode_results: bool = Query(True, alias="geocode"),
) -> dict[str, Any]:
    """One call: query NPPES, attach insurance flags (verified + estimated), then —
    for a radius search — geocode the candidate pool, keep only providers truly
    within `radius` miles of the ZIP center, sort by distance, and return ranked
    results with coordinates already attached. The backend is authoritative for the
    radius boundary; the frontend plots the returned pins without re-geocoding."""
    # A radius search widens the NPPES candidate pool (ZIP-prefix) so we can then
    # keep only providers whose geocoded address is truly within `radius` miles.
    # The pool ceiling controls recall, not the boundary — distance-filtering below
    # is the source of truth — so a wider radius gets a deeper pool.
    if radius and zip:
        pool_limit = max(limit, _POOL_CEILING_LARGE if radius > 10 else _POOL_CEILING_SMALL)
    else:
        pool_limit = limit
    q = {"zip": zip, "city": city, "state": state, "npi": npi, "name": name,
         "taxonomy": taxonomy, "type": type, "limit": pool_limit, "radius": radius}
    try:
        raw = await nppes.search(q)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        metrics.incr("upstream_error")
        # e.__class__ (not type(e)) — `type` is shadowed by the query param above.
        log.warning("provider search failed (zip=%s city=%s state=%s name=%s): %s: %s",
                    zip, city, state, name, e.__class__.__name__, e)
        raise HTTPException(502, "Could not reach the registry.") from e

    # Did the upstream NPPES pool itself hit its ceiling? If so even the post-filter
    # total below is a lower bound (there may be matches we never fetched), so the UI
    # shows "N of M+" rather than implying M is exhaustive.
    pool_capped = len(raw) >= pool_limit
    providers = [normalize(r) for r in raw]

    want = [a for a in accepts.split(",") if a.strip()]
    mode = "any" if accepts_mode == "any" else "verified"
    ann = await registry.annotate(providers, only=want or None)
    for p in providers:
        p["insurance"] = ann.get(p["npi"], {})

    # Filter: keep providers that accept ALL requested plans. In 'verified' mode an
    # estimate doesn't satisfy the filter; in 'any' mode an estimated True does.
    def accepts_plan(p: dict[str, Any], plan_id: str) -> bool:
        info = p["insurance"].get(plan_id)
        if not info or info.get("value") is not True:
            return False
        return mode == "any" or info.get("confidence") == "verified"

    # A national "operates in your area" estimate marks every in-state provider True,
    # so in 'any' mode filtering on it narrows nothing and would falsely imply the
    # kept providers were confirmed for that payer. Such plans are non-filterable
    # (filterable=False): we annotate them as context but they don't drive the filter.
    # In 'verified' mode every selected plan still filters on its verified records
    # (a payer with no verified source then honestly yields zero, not "operates here").
    plans_meta = {p["id"]: p for p in registry.plans()}
    if mode == "any":
        filter_want = [a for a in want if plans_meta.get(a, {}).get("filterable", True)]
    else:
        filter_want = list(want)
    context_plans = [a for a in want if a not in filter_want]

    # Keep providers that accept ANY of the selected (filterable) plans. Requiring ALL
    # is unintuitive and easily yields zero results (e.g. a regional payer that doesn't
    # serve the state).
    if filter_want:
        providers = [p for p in providers if any(accepts_plan(p, a) for a in filter_want)]

    # Geocode the result set when the caller wants coordinates, OR whenever this is
    # a radius search — distance-filtering needs coordinates regardless of the flag,
    # and the backend is authoritative for the boundary.
    is_radius = bool(radius and zip)
    if (geocode_results or is_radius) and providers:
        items = [{"key": p["npi"], "q": f"{p['address1']}, {p['city']}, {p['stateAb']} {p['postalCode']}"}
                 for p in providers if p["address1"] and p["city"] and p["stateAb"]]
        # Bounded so a slow/unreachable geocoder can't stall the whole search
        # past the frontend's request timeout; the cache warms over later calls.
        coords = await geocode.geocode_batch(items, budget_seconds=20)
        for p in providers:
            c = coords.get(p["npi"])
            if c:
                p["lat"], p["lng"] = c[0], c[1]

    # True radius: keep providers within `radius` miles of the ZIP center, sorted by
    # distance, BEFORE truncating to `limit` — so the closest providers survive, not
    # an arbitrary slice of the widened pool. Providers we couldn't geocode within
    # budget keep distance=None and sort last (undetermined), so a slow geocoder
    # never silently drops real results.
    if is_radius and providers:
        center = await geocode.geocode_one(zip)
        if center:
            kept = []
            for p in providers:
                if p["lat"] is None or p["lng"] is None:
                    p["distance"] = None
                    kept.append(p)
                    continue
                d = geocode.haversine_miles(center, [p["lat"], p["lng"]])
                if d <= radius:
                    p["distance"] = round(d, 1)
                    kept.append(p)
            kept.sort(key=lambda x: (x.get("distance") is None, x.get("distance") or 0))
            providers = kept

    # Cap to the requested limit. For a radius search the pool was widened and
    # distance-sorted above, so this keeps the closest `limit`; it also caps the
    # plain (no-radius) path. Capture the pre-truncation total so the UI can honestly
    # say "showing N of M" instead of silently dropping the rest.
    total_matched = len(providers)
    providers = providers[:limit]
    truncated = total_matched > len(providers)

    return {
        "count": len(providers),
        "total": total_matched,        # matches after all filters, within the fetched pool
        "truncated": truncated,        # True when results beyond `limit` were dropped
        "pool_capped": pool_capped,    # True when `total` is itself a lower bound
        "applied_filters": filter_want,  # selected plans that actually narrowed results
        "context_plans": context_plans,  # selected non-filtering "operates here" estimates
        "plans": registry.plans(),
        "providers": providers,
    }
