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
from collections.abc import Callable
from typing import Any

import httpx

from . import db, metrics, planet_registry
from .catalog import CATEGORY_ORDER, PAYER_CATALOG, category_label
from .config import settings

log = logging.getLogger("carefind.insurance")

# A single source's answer for one (provider, plan): in-network / not / unknown.
Answer = bool | None
# Per-provider lookup context: {npi: {"state": str, ...}}.
Ctx = dict[str, dict[str, Any]]

# Highest-confidence wins when several sources answer for one plan.
_CONFIDENCE_RANK = {"verified": 2, "estimated": 1}

# FHIR result <-> cache string. A True/False is a definite answer; None is "unknown"
# (the endpoint errored). These are stored distinctly so a cached "unknown" is never
# served as a "no" — it maps straight back to None and is re-fetched after a short TTL.
_FHIR_VALUE_TO_STR = {True: "in_network", False: "not_found", None: "unknown"}
_FHIR_STR_TO_VALUE = {"in_network": True, "not_found": False, "unknown": None}


def _fhir_cache_fresh(value_str: str, fetched_at: float) -> bool:
    """A cached FHIR row is fresh if its age is under the TTL for its kind — a long
    TTL for definite answers, a short one for 'unknown' so a recovered endpoint is
    retried soon instead of staying unknown."""
    ttl = (settings.fhir_cache_unknown_ttl if value_str == "unknown"
           else settings.fhir_cache_ttl)
    return (time.time() - fetched_at) < ttl

# How long a DB-backed source caches its availability. A single request calls
# plans()/categories()/annotate() (each of which checks every source's
# availability) several times — without this each call fan-outs into a COUNT(*)
# per payer. The short TTL keeps that cheap while still letting a post-startup
# ingest (e.g. `ingest_tic`) surface within a few seconds, no restart needed.
_AVAILABILITY_TTL = 5.0


def _ttl_available(holder: Any, compute: Callable[[], object]) -> bool:
    """Memoize holder._avail = (value, expires_at) for _AVAILABILITY_TTL seconds."""
    val, exp = holder._avail
    now = time.monotonic()
    if now < exp:
        return bool(val)
    try:
        val = bool(compute())
    except Exception:
        val = False
    holder._avail = (val, now + _AVAILABILITY_TTL)
    return bool(val)


class InsuranceSource:
    id = "base"            # the plan id this source answers for
    label = "Base"
    category = "commercial"
    payer = "base"
    confidence = "estimated"   # "verified" | "estimated"
    kind = "generic"
    # "payer" — the answer is about the payer's *network directory* (the provider is
    #   listed in, e.g., Aetna's network), NOT confirmation of a specific plan. A
    #   payer-level hit must never be presented as "accepts your plan".
    # "plan"  — the answer is about a specific, single plan/program (e.g. Medicare
    #   Original): a True here is a genuine plan-level confirmation.
    level = "payer"
    # A public URL a patient can follow to verify this source themselves. Verified
    # sources populate it; it backs the "Verify · checked <date>" deep link (A3).
    source_url = ""

    def available(self) -> bool:
        return False

    def discriminates(self) -> bool:
        """Whether selecting this plan as a filter can actually narrow a result set.

        Verified sources discriminate: a provider is either in the record or not.
        A national estimate that marks every provider True narrows nothing, so it is
        area *context*, not a filter (see EstimatedPayerSource).
        """
        return True

    def provenance_many(self, npis: list[str]) -> dict[str, dict[str, Any]]:
        """{str(npi): {"source_url": str, "fetched_at": float|None}} for these NPIs.

        Provenance only exists for verified sources; the estimated/base tier returns
        nothing, so an estimate can never acquire a (misleading) source link or date.
        """
        return {}

    async def check(self, npi: str) -> Answer:
        return None

    async def check_many(self, npis: list[str]) -> dict[str, Answer]:
        return {npi: await self.check(npi) for npi in npis}

    async def check_many_ctx(self, contexts: Ctx) -> dict[str, Answer]:
        """contexts: {npi: {"state": str, ...}}. Default ignores context."""
        return await self.check_many(list(contexts.keys()))


class MedicareSource(InsuranceSource):
    id = "medicare"
    label = "Medicare (Original)"
    category = "medicare"
    payer = "medicare"
    confidence = "verified"
    kind = "government"
    # Medicare FFS is a single program: enrollment in the CMS file IS plan-level
    # confirmation, not a "listed in a payer's network" signal.
    level = "plan"

    def __init__(self) -> None:
        self._avail: tuple[bool, float] = (False, 0.0)

    def available(self) -> bool:
        return _ttl_available(self, lambda: db.medicare_count() > 0)

    async def check(self, npi: str) -> Answer:
        return db.medicare_has(npi) if self.available() else None

    async def check_many(self, npis: list[str]) -> dict[str, Answer]:
        if not self.available():
            return {n: None for n in npis}
        present = db.medicare_has_many(npis)
        return {n: (n in present) for n in npis}

    def provenance_many(self, npis: list[str]) -> dict[str, dict[str, Any]]:
        meta = db.source_meta_get("medicare")
        if not meta:
            return {}
        url, ts = meta
        return {str(n): {"source_url": url, "fetched_at": ts} for n in npis}


class TicSource(InsuranceSource):
    """A commercial payer backed by an ingested Transparency-in-Coverage file.

    Each in-network NPI for the payer is stored in the SQLite `tic` table by
    (payer, npi). A hit is a verified in-network signal for that payer.
    """
    confidence = "verified"
    kind = "commercial"

    def __init__(self, payer_id: str, label: str, category: str = "commercial") -> None:
        self.id = payer_id
        self.payer = payer_id
        self.label = label
        self.category = category
        self._avail: tuple[bool, float] = (False, 0.0)

    def available(self) -> bool:
        return _ttl_available(self, lambda: db.tic_count(self.id) > 0)

    async def check(self, npi: str) -> Answer:
        return db.tic_has(self.id, npi) if self.available() else None

    async def check_many(self, npis: list[str]) -> dict[str, Answer]:
        if not self.available():
            return {n: None for n in npis}
        present = db.tic_has_many(self.id, npis)
        return {n: (n in present) for n in npis}

    def provenance_many(self, npis: list[str]) -> dict[str, dict[str, Any]]:
        meta = db.source_meta_get(self.id)
        if not meta:
            return {}
        url, ts = meta
        return {str(n): {"source_url": url, "fetched_at": ts} for n in npis}


class FhirPlanNetSource(InsuranceSource):
    """One configured payer's public FHIR Plan-Net Provider Directory (verified)."""
    confidence = "verified"
    kind = "commercial"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.id = cfg["id"]
        self.label = cfg.get("label", cfg["id"])
        self.payer = cfg.get("payer", cfg["id"])
        self.category = cfg.get("category", "commercial")
        self.base = cfg["base_url"].rstrip("/")
        self.api_key_header = cfg.get("api_key_header")
        self.api_key = cfg.get("api_key")
        self.npi_system = cfg.get("npi_system", "http://hl7.org/fhir/sid/us-npi")
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
            metrics.incr("upstream_error")
            log.warning("FHIR Plan-Net check failed (payer=%s npi=%s url=%s): %s: %s",
                        self.id, npi, url, type(e).__name__, e)
            return None
        return self._in_network(bundle)

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


class EstimatedPayerSource(InsuranceSource):
    """A curated major payer (catalog). Estimated tier only.

    Answers True when the payer operates in the provider's state (or nationally) —
    a "likely available here, confirm with the provider" signal. Never False:
    absence of a verified record is not evidence of non-acceptance.
    """
    confidence = "estimated"

    def __init__(self, entry: dict[str, Any]) -> None:
        self.id = entry["id"]
        self.payer = entry["id"]
        self.label = entry["label"]
        self.category = entry.get("category", "commercial")
        self.kind = "commercial"
        states = entry.get("states")
        self.states: set[str] | None = set(states) if states else None  # None -> national

    def available(self) -> bool:
        return True

    def discriminates(self) -> bool:
        # A national estimate (states is None) marks every provider True, so using it
        # as a filter narrows nothing and would falsely imply the kept providers were
        # confirmed for that payer. It's area context, not a filter. A regional
        # estimate genuinely discriminates by state, so it stays filterable.
        return self.states is not None

    def _serves(self, state: str) -> bool:
        if self.states is None:
            return True
        return bool(state) and state.upper() in self.states

    async def check_many_ctx(self, contexts: Ctx) -> dict[str, Answer]:
        return {
            npi: (True if self._serves((ctx or {}).get("state", "")) else None)
            for npi, ctx in contexts.items()
        }


class Registry:
    def __init__(self) -> None:
        self.sources: list[InsuranceSource] = []

    def build(self) -> None:
        sources: list[InsuranceSource] = [MedicareSource()]
        # Verified commercial payers wired via FHIR Plan-Net. Operator-configured
        # payers.json takes precedence; the validated public registry (C1) fills in the
        # rest so a fresh clone gets verified coverage out of the box. Dedup by id.
        fhir_cfgs = list(settings.load_payers())
        configured_ids = {(c or {}).get("id") for c in fhir_cfgs}
        for cfg in planet_registry.validated_payer_configs():
            if cfg["id"] not in configured_ids:
                fhir_cfgs.append(cfg)
        for cfg in fhir_cfgs:
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

    def available(self) -> list[InsuranceSource]:
        return [s for s in self.sources if s.available()]

    def _sources_by_plan(self) -> dict[str, list[InsuranceSource]]:
        by_plan: dict[str, list[InsuranceSource]] = {}
        for s in self.available():
            by_plan.setdefault(s.id, []).append(s)
        return by_plan

    def plans(self) -> list[dict[str, Any]]:
        """Flat list of filterable plans, best confidence per plan id."""
        out: list[dict[str, Any]] = []
        for plan_id, srcs in self._sources_by_plan().items():
            best = max(srcs, key=lambda s: _CONFIDENCE_RANK.get(s.confidence, 0))
            out.append({
                "id": plan_id, "label": best.label, "category": best.category,
                "payer": best.payer, "confidence": best.confidence, "kind": best.kind,
                "level": best.level, "filterable": best.discriminates(),
            })
        out.sort(key=lambda p: (CATEGORY_ORDER.get(p["category"], 99), p["label"].lower()))
        return out

    def categories(self) -> list[dict[str, Any]]:
        """Plans grouped by coverage category, in canonical display order."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in self.plans():
            groups.setdefault(p["category"], []).append(p)
        ordered = sorted(groups.items(), key=lambda kv: CATEGORY_ORDER.get(kv[0], 99))
        return [{"id": cid, "label": category_label(cid), "plans": plans} for cid, plans in ordered]

    async def check_all(self, npi: str, state: str = "") -> dict[str, Any]:
        """Single-NPI lookup. Without provider context, estimates stay unknown."""
        ann = await self.annotate([{"npi": npi, "state": state}])
        return ann.get(npi, {})

    async def annotate(
        self, providers: list[dict[str, Any]], only: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """providers: [{"npi":..., "state":...}] -> {npi: {plan_id: {value,confidence,source}}}.

        For each plan id, the best available answer across its sources wins:
        a verified True/False beats an estimate; an estimated True beats nothing.
        """
        contexts: Ctx = {}
        for p in providers:
            npi = p.get("npi")
            if npi:
                contexts[npi] = {"state": p.get("state") or p.get("stateAb") or ""}
        npis = list(contexts.keys())
        result: dict[str, dict[str, Any]] = {npi: {} for npi in npis}
        # Verified winners grouped by their source, so provenance is fetched once per
        # source rather than per result.
        verified_by_source: dict[InsuranceSource, list[tuple[str, str]]] = {}

        for plan_id, srcs in self._sources_by_plan().items():
            if only and plan_id not in only:
                continue
            # Query each source for this plan; merge by confidence then definiteness.
            per_source: list[tuple[InsuranceSource, dict[str, Answer]]] = []
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
                best: tuple[int, int, Answer, InsuranceSource] | None = None
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
                        "level": s.level,
                    }
                    if s.confidence == "verified":
                        verified_by_source.setdefault(s, []).append((npi, plan_id))

        # Stamp provenance (source URL + fetch date) onto every verified result, so a
        # green badge is always traceable to a real source — the A3 trust invariant.
        for s, winners in verified_by_source.items():
            prov = s.provenance_many([npi for npi, _ in winners])
            for npi, plan_id in winners:
                pv = prov.get(str(npi))
                if pv:
                    result[npi][plan_id]["source_url"] = pv.get("source_url", "")
                    result[npi][plan_id]["fetched_at"] = pv.get("fetched_at")
        return result


registry = Registry()
