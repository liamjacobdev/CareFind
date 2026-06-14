"""Server-side proxy to the official CMS NPPES registry API (v2.1).

The browser never calls NPPES directly when a backend is configured: this keeps
the public CORS-proxy fallbacks out of the hot path and lets us shape errors.
The query mapping mirrors the frontend's buildNpiParams() exactly so results are
identical whether the page runs standalone or backed by this API.
"""
import httpx

from .config import settings


def _wild(value: str) -> str:
    value = (value or "").strip().rstrip("*")
    return value + "*" if value else ""


def build_params(q: dict) -> dict:
    """Translate the frontend's search fields into NPPES query parameters.

    Raises ValueError when the query is too empty for NPPES to accept it.
    """
    npi = str(q.get("npi") or "").strip()

    limit = q.get("limit") or 25
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 25

    params: dict = {"version": "2.1", "limit": limit, "skip": 0}

    if npi:
        if not (npi.isdigit() and len(npi) == 10):
            raise ValueError("An NPI must be exactly 10 digits.")
        params["number"] = npi
        return params

    zip_ = str(q.get("zip") or "").strip()
    city = str(q.get("city") or "").strip()
    state = str(q.get("state") or "").strip()
    name = str(q.get("name") or "").strip()
    taxonomy = str(q.get("taxonomy") or "").strip()
    etype = str(q.get("type") or "").strip()

    try:
        radius = int(q.get("radius") or 0)
    except (TypeError, ValueError):
        radius = 0

    if zip_:
        # Real radius: widen the candidate pool beyond the exact ZIP using a
        # postal-code prefix wildcard (NPPES supports trailing '*'), then the
        # caller distance-filters geocoded results. <=10mi stays an exact match;
        # wider searches use the 3-digit ZIP prefix (~a regional cluster).
        if radius > 10 and len(zip_) == 5:
            params["postal_code"] = zip_[:3] + "*"
        else:
            params["postal_code"] = zip_
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if zip_ or city:
        params["address_purpose"] = "LOCATION"
    if taxonomy:
        params["taxonomy_description"] = taxonomy
    if etype in ("NPI-1", "NPI-2"):
        params["enumeration_type"] = etype
    if name:
        if etype == "NPI-2":
            params["organization_name"] = _wild(name)
        else:
            parts = name.split()
            if len(parts) > 1:
                params["first_name"] = _wild(parts[0])
                params["last_name"] = _wild(" ".join(parts[1:]))
            else:
                params["last_name"] = _wild(name)

    searchable = ("postal_code", "city", "state", "organization_name",
                  "first_name", "last_name", "taxonomy_description")
    if not any(k in params for k in searchable):
        raise ValueError(
            "Provide a ZIP code, a city and state, an NPI, or a name to search."
        )
    return params


async def search(q: dict) -> list:
    """Return the raw NPPES `results` list (the frontend/normalizer consumes it)."""
    import asyncio

    params = build_params(q)
    headers = {"Accept": "application/json", "User-Agent": settings.contact_ua}
    last_exc = None
    async with httpx.AsyncClient(timeout=18) as client:
        # One quick retry: the public registry occasionally throttles or drops a
        # connection on rapid repeat queries; a single retry smooths that over.
        for attempt in range(2):
            try:
                resp = await client.get(settings.nppes_base, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    await asyncio.sleep(0.8)
        else:
            raise last_exc

    if isinstance(data, dict) and data.get("Errors"):
        # NPPES reports bad queries in an Errors array — surface as a 400.
        raise ValueError(data["Errors"][0].get("description", "The registry rejected the query."))
    return data.get("results", []) if isinstance(data, dict) else []
