# CareFind vs. the alternatives

How CareFind compares to the tools people actually use to answer *"which licensed
providers near me take my insurance?"* — on **speed**, **privacy**, and **verified breadth**.

## Head-to-head

| | **CareFind** | NPPES registry site (npiregistry.cms.hhs.gov) | A typical insurer "find a doctor" directory |
|---|---|---|---|
| **Golden journey** | plan → ZIP → specialty → mapped, verified provider in **<5 interactions, <30s** (e2e-asserted) | multi-field form → paginated table, no map, no insurance | login/plan-select → directory; varies per payer |
| **Insurance signal** | **verified vs. estimated**, per-NPI, with source + fetch date; "unknown" is never a yes | none (NPPES has no plan data) | the payer's own answer — authoritative for *that* payer only |
| **Verified breadth** | Medicare (national) + ingested TiC payers + validated public FHIR Plan-Net endpoints; honest about the free-data ceiling | n/a | one payer at a time; you check each separately |
| **Map / distance** | Leaflet map, server-authoritative radius filter, distance-sorted | none | usually, within one payer |
| **Privacy** | search terms / IPs **never logged**; talks only to your backend + official registries; no trackers | government site; no accounts | accounts, marketing tags common |
| **Speed under load** | cached + circuit-broken upstreams; 60fps 1,000-row list; PWA offline shell | server-rendered, can be slow | varies |
| **Cost / openness** | **$0**, self-hostable, MIT, free public data only | free (gov) | free to members |
| **Accessibility** | WCAG 2.2 AA, axe-clean every state, keyboard-only journey (CI-gated) | basic | varies, often poor |

## Where each wins
- **NPPES site** is the system of record for *licensure/identity* — CareFind builds on the
  same data (it proxies NPPES) and adds map, distance, insurance, and UX.
- **An insurer directory** is authoritative for *its own* network — the ground truth for
  one plan. CareFind never claims to replace it; a verified hit links out to "Verify with
  <payer> · checked <date>", and estimates explicitly say "confirm with the provider."
- **CareFind** wins on a single, fast, private, accessible cross-payer search with an
  honest verified/estimated distinction and provenance on every confirmed claim.

## The honest caveat
CareFind's verified tier is bounded by what's **freely public** today (mandated FHIR
Plan-Net + public TiC). It does not have nationwide, plan-level, real-time eligibility —
that doesn't exist for free. Its edge is breadth-without-overclaim + speed + privacy +
accessibility, all at $0. See [adr/0001](adr/0001-two-tier-confidence-model.md).
