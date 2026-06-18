"""Operational + admin routes (split from main.py): liveness/freshness (/healthz),
readiness (/readyz), metrics, the verified-coverage report, and the token-secured
background ingest trigger. Not under /api/, so they aren't rate-limited."""
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Response

from . import coverage, db, metrics
from .config import settings
from .insurance import registry

router = APIRouter()
log = logging.getLogger("carefind")


def _data_freshness() -> dict[str, Any]:
    """Per-source data ages vs their SLOs (C3). A source is 'stale' when its last
    ingest is older than its budget — Medicare quarterly, TiC payers monthly. A source
    that was never ingested has no row here and so never trips the switch (that's a
    setup state, not a stalled ingest)."""
    now = time.time()
    sources: list[dict[str, Any]] = []
    stale: list[str] = []
    for sid, (_url, ts) in sorted(db.source_meta_all().items()):
        max_days = settings.medicare_max_age_days if sid == "medicare" else settings.payer_max_age_days
        age_days = (now - ts) / 86400.0
        is_stale = age_days > max_days
        sources.append({"source": sid, "age_days": round(age_days, 1),
                        "slo_days": max_days, "stale": is_stale})
        if is_stale:
            stale.append(sid)
    return {"sources": sources, "stale": stale, "slos_met": not stale}


@router.get("/healthz")
def healthz(response: Response) -> dict[str, Any]:
    """Liveness + data-freshness. Flips to 503 (the dead-man's-switch an uptime monitor
    watches) when a tracked source has gone stale past its SLO."""
    fresh = _data_freshness()
    if not fresh["slos_met"]:
        response.status_code = 503
    return {"ok": fresh["slos_met"], "medicare_npis": db.medicare_count(),
            "insurance_plans": registry.plans(), "data_freshness": fresh}


@router.get("/readyz")
def readyz(response: Response) -> dict[str, Any]:
    """Readiness (D4): is the datastore reachable and the registry built? A load
    balancer routes traffic only while this is 200, so a worker that can't reach its
    datastore is pulled instead of serving errors."""
    try:
        db.medicare_count()           # cheap datastore probe
        ready = bool(registry.sources)
    except Exception:
        ready = False
    if not ready:
        response.status_code = 503
    return {"ready": ready}


def _trigger_ingest(source: str) -> None:
    """Run the configured ingest(s) in the background (called by POST /admin/ingest).
    Imported lazily to keep startup light; failures are logged, never raised to a task."""
    from . import ingest_medicare, ingest_tic_job
    try:
        if source in ("tic", "all"):
            ingest_tic_job.run()
        if source in ("medicare", "all"):
            if settings.medicare_ingest_url:
                ingest_medicare.ingest(settings.medicare_ingest_url)
            else:
                log.warning("admin ingest: medicare skipped — CAREFIND_MEDICARE_INGEST_URL unset")
    except SystemExit as e:  # ingest_tic_job.run() exits when no sources are configured
        log.warning("admin ingest (%s) ended early: %s", source, e)
    except Exception:
        log.exception("admin ingest (%s) failed", source)


@router.post("/admin/ingest")
def admin_ingest(
    background_tasks: BackgroundTasks,
    source: str = Query("all", description="all | tic | medicare"),
    authorization: str = Header(default=""),
) -> dict[str, Any]:
    """Token-secured refresh trigger for the scheduled ingest cron. Disabled (404) until
    CAREFIND_ADMIN_TOKEN is set; the ingest runs in the background so the cron returns
    promptly. Not under /api/, so it isn't rate-limited."""
    if not settings.admin_token:
        raise HTTPException(404, "Admin ingest is disabled (set CAREFIND_ADMIN_TOKEN).")
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(403, "Invalid or missing admin token.")
    if source not in ("all", "tic", "medicare"):
        raise HTTPException(400, "source must be one of: all, tic, medicare.")
    background_tasks.add_task(_trigger_ingest, source)
    return {"status": "scheduled", "source": source}


@router.get("/metrics")
def metrics_endpoint(authorization: str = Header(default="")) -> dict[str, Any]:
    """In-process operational metrics: request counts by status, geocode/FHIR/NPPES
    cache hit rates, and upstream error count. Protected by the admin token when one is
    configured (set CAREFIND_ADMIN_TOKEN in production); open in dev when unset."""
    if settings.admin_token and authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(403, "Metrics require the admin token.")
    return metrics.snapshot()


@router.get("/coverage")
def coverage_endpoint() -> dict[str, Any]:
    """Verified-coverage-by-state report (C4): which verified programs are available in
    each state, plus the verified-record counts. Computed live, so it reflects the
    current data after every ingest."""
    return coverage.coverage_report(registry)

