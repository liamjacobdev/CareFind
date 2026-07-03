"""Insurance sources — the two-tier confidence model's source classes (split from
insurance.py to keep modules focused; re-exported by insurance.py for a stable import path).

  • "verified"  — a real source for *this* provider (Medicare file, TiC ingest, or a
      FHIR Plan-Net directory; the FHIR source lives in fhir_source.py).
  • "estimated" — a curated, clearly-labeled catalog guess. Never rendered as confirmed.

A source answers True / False / None; None ("unknown") is never turned into a yes.
"""
import logging
import time
from collections.abc import Callable
from typing import Any

from . import db
from .config import settings

log = logging.getLogger("innetwork.insurance")

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
    # Geographic scope: None -> national; a set of state codes -> regional. Verified
    # regional sources (FhirPlanNetSource) set it; used by the coverage report (C4).
    states: set[str] | None = None
    # A public URL a patient can follow to verify this source themselves. Verified
    # sources populate it; it backs the "Verify · checked <date>" deep link (A3).
    source_url = ""
    # True if answering costs a live per-NPI network round-trip (e.g. a FHIR Plan-Net
    # directory). Such sources are queried only when their plan is explicitly requested,
    # never on an unfiltered search — otherwise a default search would fire a directory
    # call for every provider in the pool. Local sources (Medicare, estimates) leave this
    # False and always annotate cheaply.
    requires_network = False

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
