"""NPPES record -> the clean provider dict the frontend renders. Split from main.py;
mirrors the frontend's buildProviders() field-for-field (the golden-fixture contract)."""
from typing import Any


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
