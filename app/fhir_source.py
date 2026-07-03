"""The FHIR Plan-Net Provider Directory source (verified tier). Split from insurance.py;
re-exported by insurance.py. Timeout-bounded + cached; presence-only is never a
Confirmed yes (see _in_network)."""
import asyncio
import logging
import time
from typing import Any

import httpx

from . import db, metrics
from .config import settings
from .oauth import ClientCredentials
from .sources import (
    _FHIR_STR_TO_VALUE,
    _FHIR_VALUE_TO_STR,
    Answer,
    Ctx,
    InsuranceSource,
    _fhir_cache_fresh,
)

log = logging.getLogger("innetwork.insurance")

class FhirPlanNetSource(InsuranceSource):
    """One configured payer's public FHIR Plan-Net Provider Directory (verified)."""
    confidence = "verified"
    kind = "commercial"
    requires_network = True  # answering = a live per-NPI directory call; query on demand only

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.id = cfg["id"]
        self.label = cfg.get("label", cfg["id"])
        self.payer = cfg.get("payer", cfg["id"])
        self.category = cfg.get("category", "commercial")
        self.base = cfg["base_url"].rstrip("/")
        self.api_key_header = cfg.get("api_key_header")
        self.api_key = cfg.get("api_key")
        # OAuth 2.0 client-credentials (for OAuth-gated payers, e.g. Aetna/Anthem). None
        # for open or static-key endpoints. Secrets come from cfg or the named env vars.
        self.oauth = ClientCredentials.from_config(cfg)
        self.npi_system = cfg.get("npi_system", "http://hl7.org/fhir/sid/us-npi")
        # "chained" (default) = one PractitionerRole?practitioner.identifier call;
        # "two_step" = resolve Practitioner by NPI then fetch its roles by reference, for
        # directories that don't support the chained search (e.g. UnitedHealthcare/Optum).
        self.lookup_mode = cfg.get("lookup_mode", "chained")
        # Regional scoping: a regional payer's directory is only queried for providers in
        # its states. Out-of-state NPIs can't be in-network there, so we return "unknown"
        # (never a fabricated answer) AND avoid hammering a regional endpoint with NPIs it
        # can't match. None -> national (query every NPI).
        states = cfg.get("states")
        self.states: set[str] | None = {s.upper() for s in states} if states else None
        # A patient-facing verify link: the payer's published directory if given,
        # else the FHIR endpoint we actually queried.
        self.source_url = cfg.get("verify_url") or cfg.get("directory_url") or self.base

    def available(self) -> bool:
        return bool(self.base)

    def provenance_many(self, npis: list[str]) -> dict[str, dict[str, Any]]:
        """Per-NPI provenance: the fetch date is the cache row's timestamp (when this
        provider's network status was actually read from the live endpoint)."""
        rows = db.fhir_cache_get_many(self.id, npis)
        out: dict[str, dict[str, Any]] = {}
        for n in npis:
            row = rows.get(str(n))
            out[str(n)] = {"source_url": self.source_url,
                           "fetched_at": row[1] if row else None}
        return out

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/fhir+json", "User-Agent": settings.contact_ua}
        if self.api_key_header and self.api_key:
            h[self.api_key_header] = self.api_key
        return h

    async def _auth_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Base headers plus, for an OAuth-gated payer, a Bearer token (cached). A token
        fetch failure leaves the Authorization header off → the request 401s → the source
        degrades to 'unknown', never a fabricated answer."""
        h = self._headers()
        if self.oauth is not None:
            tok = await self.oauth.token_async(client)
            if tok:
                h["Authorization"] = f"Bearer {tok}"
        return h

    @staticmethod
    def _has_network_link(res: dict[str, Any]) -> bool:
        """True only if this PractitionerRole carries a *resolvable network reference*.

        In FHIR Plan-Net a provider's participation is asserted by linking the role to
        a `Network` resource (the `network` field, or a Plan-Net `network-reference`
        extension carrying a `valueReference`). `healthcareService` is NOT a network
        link — a provider can offer services without being in any network — so it is
        deliberately excluded. Absent a real network reference we cannot confirm
        in-network status, only directory presence.
        """
        for ref in res.get("network", []) or []:
            if isinstance(ref, dict) and ref.get("reference"):
                return True
        for ext in res.get("extension", []) or []:
            if not isinstance(ext, dict):
                continue
            if "network" in (ext.get("url", "") or "").lower():
                vr = ext.get("valueReference")
                if isinstance(vr, dict) and vr.get("reference"):
                    return True
        return False

    @classmethod
    def _in_network(cls, bundle: dict[str, Any], strictness: str | None = None) -> Answer:
        """Map a PractitionerRole Bundle to True / False / None (unknown).

        Trust invariant: directory *presence* alone never becomes a Confirmed "yes".

          • True  — an active PractitionerRole with a resolvable network link (or, in
                    "directory" strictness, any active PractitionerRole).
          • None  — the provider is *listed* (≥1 PractitionerRole) but no active role
                    carries a network link: we can't confirm the network, so "unknown".
          • False — the provider is not in this payer's directory at all (no role).

        `active: false` roles are never treated as a yes; a bundle of only-inactive
        roles is therefore "listed but unconfirmable" → None, not False.
        """
        strictness = strictness or settings.fhir_strictness
        entries = bundle.get("entry", []) if isinstance(bundle, dict) else []
        saw_role = False
        saw_active = False
        for e in entries:
            res = e.get("resource", {})
            if res.get("resourceType") != "PractitionerRole":
                continue
            saw_role = True
            if res.get("active") is False:
                continue
            saw_active = True
            if cls._has_network_link(res):
                return True
        if strictness == "directory" and saw_active:
            return True
        if saw_role:
            return None  # listed, but presence alone can't confirm in-network
        return False  # not in the directory at all

    async def _check(self, client: httpx.AsyncClient, npi: str) -> Answer:
        if self.lookup_mode == "two_step":
            return await self._check_two_step(client, npi)
        url = f"{self.base}/PractitionerRole"
        params = {"practitioner.identifier": f"{self.npi_system}|{npi}", "_count": "5"}
        try:
            r = await client.get(url, params=params, headers=await self._auth_headers(client))
            if r.status_code == 404:
                return False
            r.raise_for_status()
            bundle = r.json()
        except Exception as e:
            # Degrade to "unknown" (never a fabricated yes), but log which payer/NPI
            # endpoint failed so a down or misconfigured Plan-Net directory is visible.
            metrics.incr("upstream_error")
            log.warning("FHIR Plan-Net check failed (payer=%s npi=%s url=%s): %s: %s",
                        self.id, npi, url, type(e).__name__, e)
            return None
        return self._in_network(bundle)

    async def _check_two_step(self, client: httpx.AsyncClient, npi: str) -> Answer:
        """For directories without the chained practitioner.identifier search: resolve the
        Practitioner by NPI, then fetch its PractitionerRoles by reference and judge those.

        Trust-preserving: a bogus NPI resolves to no Practitioner -> False (never a yes);
        the in-network determination still runs `_in_network` over the real roles, so a
        listed-but-not-network-linked provider is "unknown", never a fabricated yes."""
        try:
            headers = await self._auth_headers(client)
            pr = await client.get(f"{self.base}/Practitioner", headers=headers,
                                  params={"identifier": f"{self.npi_system}|{npi}", "_count": "5"})
            if pr.status_code == 404:
                return False
            pr.raise_for_status()
            prac_ids = [res["id"] for e in (pr.json().get("entry") or [])
                        if (res := e.get("resource") or {}).get("resourceType") == "Practitioner"
                        and res.get("id")]
            if not prac_ids:
                return False  # NPI is not in this directory at all
            entries: list[dict[str, Any]] = []
            for pid in prac_ids[:3]:  # an NPI maps to ~1 practitioner; cap fan-out anyway
                rb = await client.get(f"{self.base}/PractitionerRole", headers=headers,
                                      params={"practitioner": f"Practitioner/{pid}", "_count": "10"})
                rb.raise_for_status()
                entries.extend(rb.json().get("entry") or [])
            return self._in_network({"entry": entries})
        except Exception as e:
            metrics.incr("upstream_error")
            log.warning("FHIR Plan-Net 2-step check failed (payer=%s npi=%s): %s: %s",
                        self.id, npi, type(e).__name__, e)
            return None

    async def check(self, npi: str) -> Answer:
        # Route through the cached batch path so a single lookup is cached too.
        return (await self.check_many([npi])).get(npi)

    async def check_many(self, npis: list[str]) -> dict[str, Answer]:
        """Concurrent, bounded, and cached — so N payers x N providers can't serialize
        into a request timeout, and a warm search makes zero live FHIR calls.

        Fresh cache hits are served from SQLite; only the misses (and stale rows) hit
        the live endpoint, and their results are persisted. A cached 'unknown' is
        re-fetched after a short TTL and is never treated as 'not in network'."""
        out: dict[str, Answer] = {n: None for n in npis}
        if not npis:
            return out

        # 1) Serve fresh cache hits; collect the rest to fetch live.
        cached = db.fhir_cache_get_many(self.id, npis)
        to_fetch: list[str] = []
        for n in npis:
            row = cached.get(str(n))
            if row is not None:
                value_str, fetched_at = row
                if _fhir_cache_fresh(value_str, fetched_at):
                    out[n] = _FHIR_STR_TO_VALUE.get(value_str)
                    metrics.incr("fhir_hit")
                    continue
            to_fetch.append(n)
        metrics.incr("fhir_miss", len(to_fetch))
        if not to_fetch:
            return out

        # 2) Fetch the misses live, bounded-concurrently, then persist every result
        #    (including 'unknown', so a flapping endpoint isn't hammered every search).
        sem = asyncio.Semaphore(8)
        fetched: dict[str, Answer] = {}
        async with httpx.AsyncClient(timeout=12) as client:
            async def one(n: str) -> tuple[str, Answer]:
                async with sem:
                    return n, await self._check(client, n)
            for res in await asyncio.gather(*[one(n) for n in to_fetch], return_exceptions=True):
                if isinstance(res, BaseException):
                    continue
                n, v = res
                out[n] = v
                fetched[n] = v
        if fetched:
            now = time.time()
            db.fhir_cache_set_many(
                self.id, [(n, _FHIR_VALUE_TO_STR.get(v, "unknown")) for n, v in fetched.items()], now
            )
        return out

    async def check_many_ctx(self, contexts: Ctx) -> dict[str, Answer]:
        """Regional scoping: only live-query providers in this payer's states. A national
        payer (states is None) queries everyone; a regional one returns "unknown" for
        out-of-state NPIs without a live call — never a fabricated answer, and no needless
        load on a regional directory."""
        if self.states is None:
            return await self.check_many(list(contexts.keys()))
        in_state = [npi for npi, ctx in contexts.items()
                    if (ctx or {}).get("state", "").upper() in self.states]
        out: dict[str, Answer] = {npi: None for npi in contexts}
        out.update(await self.check_many(in_state))
        return out
