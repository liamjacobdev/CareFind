"""Insurance acceptance — a two-tier confidence model over real + curated sources.

Every answer CareFind gives about a plan is tagged with a confidence:

  • "verified"  — confirmed from a real source for *this* provider:
      - MedicareSource: the CMS Medicare Fee-For-Service enrollment file (by NPI).
      - FhirPlanNetSource: a payer's CMS-mandated public FHIR Plan-Net directory.
      - TicSource: an ingested Transparency-in-Coverage in-network file (by NPI).
  • "estimated" — a curated, clearly-labeled guess from EstimatedPayerSource: a
      major payer that operates in the provider's state. Never presented as
      confirmed; the UI shows it as "likely — confirm with the provider."

A plan is identified by a stable `id`; several sources may answer for the same id
(e.g. a verified TiC ingest and an estimated catalog entry for "aetna"). The
Registry merges them and a verified answer always wins over an estimate. A source
may answer True / False / None; None ("unknown") is never turned into a yes.

Per-provider results have the shape:
    {plan_id: {"value": True|False|None, "confidence": "verified"|"estimated",
               "source": <source id>}}
"""
import asyncio
import logging
import time

import httpx

from . import db
from .catalog import CATEGORY_ORDER, PAYER_CATALOG, category_label
from .config import settings

log = logging.getLogger("carefind.insurance")

# Highest-confidence wins when several sources answer for one plan.
_CONFIDENCE_RANK = {"verified": 2, "estimated": 1}

# How long a DB-backed source caches its availability. A single request calls
# plans()/categories()/annotate() (each of which checks every source's
# availability) several times — without this each call fan-outs into a COUNT(*)
# per payer. The short TTL keeps that cheap while still letting a post-startup
# ingest (e.g. `ingest_tic`) surface within a few seconds, no restart needed.
_AVAILABILITY_TTL = 5.0


def _ttl_available(holder, compute) -> bool:
    """Memoize holder._avail = (value, expires_at) for _AVAILABILITY_TTL seconds."""
    val, exp = holder._avail
    now = time.monotonic()
    if now < exp:
        return val
    try:
        val = bool(compute())
    except Exception:
        val = False
    holder._avail = (val, now + _AVAILABILITY_TTL)
    return val


class InsuranceSource:
    id = "base"            # the plan id this source answers for
    label = "Base"
    category = "commercial"
    payer = "base"
    confidence = "estimated"   # "verified" | "estimated"
    kind = "generic"

    def available(self) -> bool:
        return False

    async def check(self, npi: str):
        return None

    async def check_many(self, npis: list) -> dict:
        return {npi: await self.check(npi) for npi in npis}

    async def check_many_ctx(self, contexts: dict) -> dict:
        """contexts: {npi: {"state": str, ...}}. Default ignores context."""
        return await self.check_many(list(contexts.keys()))


class MedicareSource(InsuranceSource):
    id = "medicare"
    label = "Medicare (Original)"
    category = "medicare"
    payer = "medicare"
    confidence = "verified"
    kind = "government"

    def __init__(self):
        self._avail = (False, 0.0)

    def available(self) -> bool:
        return _ttl_available(self, lambda: db.medicare_count() > 0)

    async def check(self, npi: str):
        return db.medicare_has(npi) if self.available() else None

    async def check_many(self, npis: list) -> dict:
        if not self.available():
            return {n: None for n in npis}
        present = db.medicare_has_many(npis)
        return {n: (n in present) for n in npis}


class TicSource(InsuranceSource):
    """A commercial payer backed by an ingested Transparency-in-Coverage file.

    Each in-network NPI for the payer is stored in the SQLite `tic` table by
    (payer, npi). A hit is a verified in-network signal for that payer.
    """
    confidence = "verified"
    kind = "commercial"

    def __init__(self, payer_id: str, label: str, category: str = "commercial"):
        self.id = payer_id
        self.payer = payer_id
        self.label = label
        self.category = category
        self._avail = (False, 0.0)

    def available(self) -> bool:
        return _ttl_available(self, lambda: db.tic_count(self.id) > 0)

    async def check(self, npi: str):
        return db.tic_has(self.id, npi) if self.available() else None

    async def check_many(self, npis: list) -> dict:
        if not self.available():
            return {n: None for n in npis}
        present = db.tic_has_many(self.id, npis)
        return {n: (n in present) for n in npis}


class FhirPlanNetSource(InsuranceSource):
    """One configured payer's public FHIR Plan-Net Provider Directory (verified)."""
    confidence = "verified"
    kind = "commercial"

    def __init__(self, cfg: dict):
        self.id = cfg["id"]
        self.label = cfg.get("label", cfg["id"])
        self.payer = cfg.get("payer", cfg["id"])
        self.category = cfg.get("category", "commercial")
        self.base = cfg["base_url"].rstrip("/")
        self.api_key_header = cfg.get("api_key_header")
        self.api_key = cfg.get("api_key")
        self.npi_system = cfg.get("npi_system", "http://hl7.org/fhir/sid/us-npi")

    def available(self) -> bool:
        return bool(self.base)

    def _headers(self):
        h = {"Accept": "application/fhir+json", "User-Agent": settings.contact_ua}
        if self.api_key_header and self.api_key:
            h[self.api_key_header] = self.api_key
        return h

    @staticmethod
    def _in_network(bundle: dict) -> bool:
        """An active PractitionerRole with a network reference = in-network."""
        entries = bundle.get("entry", []) if isinstance(bundle, dict) else []
        for e in entries:
            res = e.get("resource", {})
            if res.get("resourceType") != "PractitionerRole":
                continue
            if res.get("active") is False:
                continue
            has_network = any(
                "network" in (ext.get("url", "").lower())
                for ext in res.get("extension", [])
            ) or bool(res.get("network")) or bool(res.get("healthcareService"))
            if has_network:
                return True
        return False

    async def _check(self, client: httpx.AsyncClient, npi: str):
        url = f"{self.base}/PractitionerRole"
        params = {"practitioner.identifier": f"{self.npi_system}|{npi}", "_count": "5"}
        try:
            r = await client.get(url, params=params, headers=self._headers())
            if r.status_code == 404:
                return False
            r.raise_for_status()
            bundle = r.json()
        except Exception as e:
            # Degrade to "unknown" (never a fabricated yes), but log which payer/NPI
            # endpoint failed so a down or misconfigured Plan-Net directory is visible.
            log.warning("FHIR Plan-Net check failed (payer=%s npi=%s url=%s): %s: %s",
                        self.id, npi, url, type(e).__name__, e)
            return None
        return self._in_network(bundle)

    async def check(self, npi: str):
        async with httpx.AsyncClient(timeout=12) as client:
            return await self._check(client, npi)

    async def check_many(self, npis: list) -> dict:
        """Concurrent, bounded — so N payers x N providers can't serialize into a
        request timeout. Shares one client and caps in-flight requests."""
        out = {n: None for n in npis}
        if not npis:
            return out
        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=12) as client:
            async def one(n):
                async with sem:
                    return n, await self._check(client, n)
            for res in await asyncio.gather(*[one(n) for n in npis], return_exceptions=True):
                if isinstance(res, Exception):
                    continue
                n, v = res
                out[n] = v
        return out


class EstimatedPayerSource(InsuranceSource):
    """A curated major payer (catalog). Estimated tier only.

    Answers True when the payer operates in the provider's state (or nationally) —
    a "likely available here, confirm with the provider" signal. Never False:
    absence of a verified record is not evidence of non-acceptance.
    """
    confidence = "estimated"

    def __init__(self, entry: dict):
        self.id = entry["id"]
        self.payer = entry["id"]
        self.label = entry["label"]
        self.category = entry.get("category", "commercial")
        self.kind = "commercial"
        states = entry.get("states")
        self.states = set(states) if states else None  # None -> national

    def available(self) -> bool:
        return True

    def _serves(self, state: str) -> bool:
        if self.states is None:
            return True
        return bool(state) and state.upper() in self.states

    async def check_many_ctx(self, contexts: dict) -> dict:
        return {
            npi: (True if self._serves((ctx or {}).get("state", "")) else None)
            for npi, ctx in contexts.items()
        }


class Registry:
    def __init__(self):
        self.sources = []

    def build(self):
        sources = [MedicareSource()]
        # Verified commercial payers wired via FHIR Plan-Net (payers.json).
        for cfg in settings.load_payers():
            try:
                sources.append(FhirPlanNetSource(cfg))
            except Exception as e:
                # A malformed payers.json entry shouldn't kill startup, but it must
                # not vanish silently either — name the offending entry.
                log.warning("Skipping FHIR payer entry %r: %s: %s",
                            (cfg or {}).get("id", cfg), type(e).__name__, e)
        # Verified commercial payers ingested from Transparency-in-Coverage files.
        # Always register one TiC source per catalog payer; availability is checked
        # live (with a short TTL) like Medicare, so a payer ingested AFTER startup
        # surfaces within seconds instead of waiting for a server restart.
        for entry in PAYER_CATALOG:
            sources.append(
                TicSource(entry["id"], entry["label"], entry.get("category", "commercial"))
            )
        # Estimated tier: every catalog payer is also offered as a labeled estimate.
        for entry in PAYER_CATALOG:
            sources.append(EstimatedPayerSource(entry))
        self.sources = sources

    def available(self):
        return [s for s in self.sources if s.available()]

    def _sources_by_plan(self):
        by_plan = {}
        for s in self.available():
            by_plan.setdefault(s.id, []).append(s)
        return by_plan

    def plans(self):
        """Flat list of filterable plans, best confidence per plan id."""
        out = []
        for plan_id, srcs in self._sources_by_plan().items():
            best = max(srcs, key=lambda s: _CONFIDENCE_RANK.get(s.confidence, 0))
            out.append({
                "id": plan_id, "label": best.label, "category": best.category,
                "payer": best.payer, "confidence": best.confidence, "kind": best.kind,
            })
        out.sort(key=lambda p: (CATEGORY_ORDER.get(p["category"], 99), p["label"].lower()))
        return out

    def categories(self):
        """Plans grouped by coverage category, in canonical display order."""
        groups = {}
        for p in self.plans():
            groups.setdefault(p["category"], []).append(p)
        ordered = sorted(groups.items(), key=lambda kv: CATEGORY_ORDER.get(kv[0], 99))
        return [{"id": cid, "label": category_label(cid), "plans": plans} for cid, plans in ordered]

    async def check_all(self, npi: str, state: str = "") -> dict:
        """Single-NPI lookup. Without provider context, estimates stay unknown."""
        ann = await self.annotate([{"npi": npi, "state": state}])
        return ann.get(npi, {})

    async def annotate(self, providers: list, only=None) -> dict:
        """providers: [{"npi":..., "state":...}] -> {npi: {plan_id: {value,confidence,source}}}.

        For each plan id, the best available answer across its sources wins:
        a verified True/False beats an estimate; an estimated True beats nothing.
        """
        contexts = {}
        for p in providers:
            npi = p.get("npi")
            if npi:
                contexts[npi] = {"state": p.get("state") or p.get("stateAb") or ""}
        npis = list(contexts.keys())
        result = {npi: {} for npi in npis}

        for plan_id, srcs in self._sources_by_plan().items():
            if only and plan_id not in only:
                continue
            # Query each source for this plan; merge by confidence then definiteness.
            per_source = []
            for s in srcs:
                try:
                    per_source.append((s, await s.check_many_ctx(contexts)))
                except Exception as e:
                    # One source failing degrades its plan to "unknown" for this
                    # batch; the rest still answer. Record which source broke.
                    log.warning("Insurance source %r failed for %d NPIs: %s: %s",
                                s.id, len(npis), type(e).__name__, e)
                    per_source.append((s, {n: None for n in npis}))
            for npi in npis:
                best = None  # (rank, definite, value, source)
                for s, m in per_source:
                    v = m.get(npi)
                    if v is None:
                        continue
                    rank = _CONFIDENCE_RANK.get(s.confidence, 0)
                    cand = (rank, 1 if v is True else 0, v, s)
                    if best is None or cand[:2] > best[:2]:
                        best = cand
                if best is not None:
                    _, _, value, s = best
                    result[npi][plan_id] = {
                        "value": value, "confidence": s.confidence, "source": s.id,
                    }
        return result


registry = Registry()
