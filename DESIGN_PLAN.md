# CareFind — Visual & UI Execution Plan to 10/10 (for Claude Opus 4.8)

> **Scope:** This plan is about *how the product looks and feels* — the pixels, the
> motion, the type, the responsive behavior. It is the visual/UX counterpart to the
> trust/data-focused `PLAN.md` and does **not** restate it. Where the two touch
> (e.g. badge semantics), trust copy is fixed; this plan only changes its *presentation*.
>
> **This document is self-contained.** A fresh session can execute it cold. Read
> §0–§2, then execute phases **V0 → V7 in order**. Every task has a binary
> **acceptance gate**. A task is done only when its gate passes via a documented,
> repeatable command or a committed visual artifact.
>
> **Constraints inherited from PLAN.md (non-negotiable):** $0 forever; single-file
> deploy (`carefind.html` + bundle); never weaken a trust claim; never add a runtime
> dependency that isn't free and pinned. No CSS framework, no component library — the
> design system is hand-authored CSS tokens. The current bundle/CSP discipline stays.

---

## 0. Mission & current state

**Mission.** Make CareFind *look* as trustworthy as it *is* — a tool a clinician or a
scared patient would believe on sight, and that a designer at Linear/Stripe/Apple
would not be able to fault. The aesthetic target: **calm institutional credibility
with one or two moments of genuine craft**, not flashy SaaS.

**Where it is today (honest baseline — audited 2026-06-19):** a genuinely good
single-page app. Coherent forest-ink + jade + honey identity, Fraunces/Inter pairing,
designed empty/error/loading states, real motion, strong keyboard a11y. It already
clears most production bars. It is **not** yet top-0.1%. The gaps are specific and
fixable:

| Theme | Concrete defect (verified) |
|---|---|
| **No dark mode** | `grep prefers-color-scheme carefind.html` → 0. A 2026 health tool with no dark theme. |
| **Ad-hoc type scale** | 25 distinct `rem` font-sizes (`.58`→`1.52`), no modular ratio. Steps like `.69`/`.7`/`.72` are indistinguishable noise. |
| **Ad-hoc spacing** | Padding/margins are arbitrary (`.78rem`, `1.05rem`, `.95rem`…), not a 4/8px grid. No spacing tokens exist. |
| **One breakpoint** | Single `@media (max-width:920px)`. No tablet tuning; map is `display:none` on mobile (toggle-only, second-class). |
| **Mobile header breaks** | The "Live national registry" pill wraps to 3 lines; "Source: CMS NPPES" wraps. Cramped. |
| **Card density** | Footer competes: "Official record" + insurance badge + "Mapped" + distance all at once. No clear primary. |
| **Generated avatar colors** | Hash → palette can yield muddy/low-contrast fills under white text; not contrast-guaranteed. |
| **Insurance filter busy** | Mode toggle + legend + grouped chips + confidence dots in a small column — powerful but visually loud. |
| **Incomplete token system** | Color tokens exist; spacing, type, radius-usage, z-index, motion are partly magic numbers. |
| **Map under-branded** | Default CARTO tiles + generic pins; doesn't carry the brand the rest of the app does. |

**Baseline score: ~7.5/10** (see §1 scorecard). The job is to close every row above
and add two signature moments, verifiably.

---

## 1. The scorecard we are grading against (extreme bar = Linear/Stripe/Things/Apple)

| # | Category | Now | 10 means |
|---|----------|-----|----------|
| 1 | Visual design & brand identity | 8.0 | Distinctive, confident, one signature moment; map carries the brand. |
| 2 | Typography | 7.5 | A disciplined modular scale (≤8 steps), correct optical sizing & rhythm everywhere. |
| 3 | Color & contrast | 7.0 | Full semantic token system + first-class dark mode; every pair ≥ WCAG AA, decorative ≥3:1, avatars contrast-guaranteed. |
| 4 | Layout, spacing & composition | 7.5 | Strict 4pt spacing scale; flawless composition at every width; no cramped seams. |
| 5 | Design system & consistency | 7.0 | Every visual decision is a token; zero magic numbers in component CSS; documented. |
| 6 | Motion & microinteractions | 8.0 | View Transitions on nav; shared-element card→drawer; nothing janky; reduced-motion parity. |
| 7 | Information hierarchy & data presentation | 8.0 | One clear primary per card; badge system with explicit rank; scannable at a glance. |
| 8 | Interaction & affordances | 8.0 | Every control obvious, reversible, and quick; inline validation; thumb-reachable on mobile. |
| 9 | Responsive & cross-device | 6.5 | Purposeful mobile/tablet/desktop layouts; map is first-class on mobile; container queries. |
| 10 | States & feedback | 8.5 | Already strong — preserve, extend to dark mode + new components, add optimistic polish. |

**Overall ≈ 7.5.** Definition of 10 = §2 gates all green, recorded in `docs/design-audit.md`.

---

## 2. The Definition of "10" — per-category visual gates

A category is 10 **only** when its gate is objectively satisfied.

| # | Gate (must all pass) |
|---|----------------------|
| 1 | Brand: committed before/after of a redesigned, brand-themed map + a documented "signature moment"; design-principles doc committed. |
| 2 | Type: all font-sizes resolve to `var(--text-*)` tokens on a documented modular scale (≤8 steps); CI greps for raw `font-size:[0-9]` in component CSS → 0 hits. |
| 3 | Color: dark mode ships and is toggle- + system-driven; automated contrast test passes for 100% of text pairs (AA) and avatars; no raw hex in component rules (tokens only). |
| 4 | Spacing: all padding/margin/gap resolve to `var(--space-*)` (4pt scale); CI grep for raw `rem`/`px` spacing in component CSS → 0 hits; visual-regression at 360/768/1024/1440 all green. |
| 5 | System: `docs/design-system.md` documents tokens + component variants; a token-lint CI step proves component CSS uses only tokens. |
| 6 | Motion: View Transitions API drives tab + list↔map + card→drawer; profiled 60fps; reduced-motion path verified by test; no layout shift on transition (CLS≈0). |
| 7 | Hierarchy: each card has exactly one visually-primary action/datum (eye-track/spec'd); badge rank documented and implemented; usability check on 3 users or a heuristic checklist committed. |
| 8 | Interaction: inline field validation (no error relies on a toast alone); all targets ≥44px; golden journey completed one-handed on mobile (recorded). |
| 9 | Responsive: distinct, intentional layouts at ≤480 / 481–1024 / >1024; map reachable & usable on mobile without losing the list; container queries where component-local. |
| 10 | States: every state (welcome/loading/empty/error/backend-required/no-results/saved-empty) renders correctly in **both** themes; visual-regression covers each. |

---

## 3. Phased execution — V0 → V7 in order

Each task: **Goal · Files · Steps · Gate · Commit.** Within a phase, tasks may
parallelize unless a dependency is noted. **V0 is the bedrock and blocks the rest** —
do not skip it to chase a visible win; every later phase consumes its tokens.

---

### PHASE V0 — Tokenize the system (bedrock; blocks everything)

> First principles: you cannot make 25 font sizes and 40 spacing values *consistent*
> by editing them one at a time. Define the system once; everything downstream becomes
> a token swap. This phase changes **no pixels** by design — it's a pure refactor that
> must render byte-identically before/after, proven by visual regression.

#### V0.1 — Establish the full token layer
- **Goal:** One `:root` block that is the single source of every visual constant:
  spacing (4pt scale), type scale, line-heights, radii, z-index ladder, motion
  (durations + easings), and semantic color roles (not just raw palette).
- **Files:** `carefind.html` (`<style>` `:root`), new `docs/design-system.md`.
- **Steps:**
  1. **Spacing** — define `--space-1…12` on a 4pt base (`4,8,12,16,20,24,32,40,48,64`).
  2. **Type** — collapse the 25 sizes to a documented modular scale of ≤8 steps
     (`--text-2xs … --text-3xl`, e.g. ~1.16 ratio: `11,12,13,14,16,19,23,28`px) plus
     `--leading-tight/normal/relaxed`. Map every existing size to the nearest step;
     record the mapping in the doc.
  3. **Radii / z-index / motion** — `--r-sm/md/lg/full`; `--z-header/map/scrim/drawer/modal/toast`; `--dur-fast/base/slow` + the two existing easings as `--ease`/`--ease-out`.
  4. **Semantic color roles** — introduce role tokens that *reference* the palette:
     `--bg`, `--bg-elevated`, `--surface`, `--surface-2`, `--text-primary`,
     `--text-secondary`, `--text-faint`, `--border`, `--accent`, `--accent-strong`,
     `--positive`, `--warning`, `--danger`, `--focus`. Components will consume **roles**, never raw palette — this is what makes V1 dark mode a 30-line diff.
- **Gate:** `docs/design-system.md` committed with the full token table + type/spacing
  mapping. App renders pixel-identical (visual-regression diff = 0 — set up in V7.1 first
  if needed, or eyeball-commit with screenshots in this PR).
- **Commit:** `V0.1: introduce spacing/type/radius/z/motion/color-role token layer`

#### V0.2 — Migrate component CSS onto roles + tokens (no visual change)
- **Goal:** Every rule in the component CSS uses tokens, not literals. After this,
  a literal `font-size:.7rem` or `padding:11px` in a component rule is a CI failure.
- **Files:** `carefind.html` (`<style>` all component blocks), `src/main.js` &
  `carefind.logic.js` (inline `style.cssText`/`innerHTML` style strings → CSS classes
  or token-driven custom properties; e.g. `showMapUnavailable`, skeleton inline styles).
- **Steps:**
  1. Replace raw `font-size`, `padding`, `margin`, `gap`, `border-radius`, color hex,
     and `z-index` in every `.class` rule with the V0.1 tokens (snap to nearest step).
  2. Hoist the JS-injected inline styles (skeletons, map-unavailable, drawer body
     fragments) into real classes so they're tokenized and themeable too.
  3. Add `scripts/lint-tokens.mjs`: greps component CSS for raw `font-size:[0-9]`,
     spacing literals, and `#[0-9a-f]{3,6}` outside the `:root` palette → exits nonzero.
- **Gate:** `node scripts/lint-tokens.mjs` clean; visual-regression diff ≈ 0 vs V0.1;
  `npm run build` still reproduces the bundle; existing Playwright/Vitest green.
- **Commit:** `V0.2: migrate all component CSS to design tokens; add token-lint gate`

---

### PHASE V1 — Color, contrast & first-class dark mode (Category 3, 10)

#### V1.1 — Ship dark mode (system + manual toggle)
- **Goal:** A genuine, designed dark theme — not an inverted hack. Driven by
  `prefers-color-scheme` and an explicit user toggle persisted to `localStorage`.
- **Files:** `carefind.html` (`:root[data-theme]` blocks, header toggle button),
  `src/main.js` (toggle wiring + persistence + `theme-color` meta swap), `src/config.js`.
- **Steps:**
  1. Add a `@media (prefers-color-scheme: dark)` mapping **and** a
     `:root[data-theme="dark"]` override, both re-pointing the V0.1 **role** tokens to a
     dark palette (warm-dark ink surfaces, lifted jade for accent legibility, dimmed
     honey). Component CSS doesn't change — it already consumes roles.
  2. Header toggle (sun/moon, accessible name, `aria-pressed`); persist choice;
     default to system. Swap `<meta name="theme-color">` per theme.
  3. Re-theme the **map** for dark (CARTO `dark_matter` tiles when dark) and the
     Leaflet popups/markers via role tokens.
- **Gate:** Toggle + system both work; every state renders correctly dark (V7 covers
  it); `theme-color` updates; map tiles switch. Recorded before/after screenshots.
- **Commit:** `V1.1: first-class dark mode via color-role tokens + map theme`

#### V1.2 — Contrast & avatar-color guarantees
- **Goal:** No text pair below AA; decorative borders ≥3:1; avatar fills always pass
  white-text contrast in both themes.
- **Files:** `carefind.logic.js` (avatar color generation — the `PALETTE`/hash), new
  `tests-js/contrast.test.js`, `scripts/contrast-audit.mjs`.
- **Steps:**
  1. Replace the free-hash avatar color with a curated, contrast-verified palette (or
     clamp generated HSL lightness so white text always clears 4.5:1). Keep deterministic
     per-NPI assignment.
  2. `scripts/contrast-audit.mjs`: enumerate every role-token text/bg pair (light + dark)
     and every avatar palette entry; assert AA (or AAA for body); fail CI otherwise.
- **Gate:** Contrast audit green for 100% of pairs in both themes; avatar test proves
  every palette entry ≥4.5:1 with white. axe still 0 violations.
- **Commit:** `V1.2: contrast-guaranteed avatars + automated AA contrast gate`

---

### PHASE V2 — Typography discipline (Category 2)

#### V2.1 — Enforce the modular scale + rhythm
- **Goal:** Type reads as a system: ≤8 sizes, correct line-heights, optical sizing,
  tabular numerals where data aligns (distances, NPI, counts).
- **Files:** `carefind.html` (`<style>`), `src/main.js`/`carefind.logic.js` (any inline
  type), `docs/design-system.md`.
- **Steps:**
  1. Confirm every `font-size` is a `--text-*` token (V0.2 did the swap; here you
     *tune* the scale values so the hierarchy is unmistakable — e.g. card name vs
     specialty vs meta must be clearly distinct steps, not `.9`/`.74`).
  2. Pair each size with an intended `--leading-*`; fix cramped/loose blocks (the
     `.data-note` paragraph, drawer rows).
  3. Add `font-variant-numeric: tabular-nums` to distances, NPI, counts, dates so
     numbers don't jitter.
  4. Verify Fraunces optical sizing (`opsz`) is applied at display sizes; ensure the
     wordmark and section headers use it.
- **Gate:** CI grep proves no raw `font-size:[0-9]` in component CSS; documented scale
  in the design-system doc; visual-regression approved (intentional diffs).
- **Commit:** `V2.1: enforce modular type scale, line-height rhythm, tabular numerals`

---

### PHASE V3 — Layout, spacing & responsive (Categories 4, 9)

#### V3.1 — Strict spacing & composition pass
- **Goal:** Every gap/padding/margin on the 4pt scale; no cramped or arbitrary seams;
  optical alignment across the search panel, cards, drawer, modal.
- **Files:** `carefind.html` (`<style>`), inline style fragments in `src/main.js`.
- **Steps:** Snap all spacing to `--space-*`; rationalize the search panel vertical
  rhythm; align card avatar/text/badges to a consistent baseline; equalize drawer
  section spacing.
- **Gate:** Token-lint proves no raw spacing literals in component CSS; visual-regression
  at all widths approved.
- **Commit:** `V3.1: snap all spacing to the 4pt scale; composition pass`

#### V3.2 — Real responsive system (mobile/tablet/desktop) + mobile map
- **Goal:** Three intentional layouts, not one breakpoint. Fix the mobile header.
  Make the map a first-class citizen on mobile instead of `display:none`.
- **Files:** `carefind.html` (`<style>` media/container queries, header markup),
  `src/main.js` (`setView`, map invalidation, bottom-sheet logic).
- **Steps:**
  1. **Header** — at ≤480px, drop/shrink the credential pill to a single token or move
     it below the wordmark; never let it wrap to 3 lines. "Source" link → icon-only.
  2. **Breakpoints** — introduce ≤480 (phone), 481–1024 (tablet: keep split but
     re-proportion, e.g. 40/60), >1024 (desktop). Add **container queries** for the
     card grid so cards reflow by their own column width, not the viewport.
  3. **Mobile map** — replace the hard List/Map swap with a draggable **bottom sheet**:
     map fills the screen, results live in a sheet that snaps peek/half/full. The
     selected card and the active pin stay in sync. (If a sheet is too costly, at
     minimum cross-fade the swap and keep a persistent mini-map peek.)
- **Gate:** Visual-regression at 360/768/1024/1440 all approved; one-handed mobile
  golden journey recorded; header never wraps; map usable on mobile with list retained.
- **Commit:** `V3.2: phone/tablet/desktop layouts, container-query cards, mobile map sheet`

---

### PHASE V4 — Component refinement (Categories 6, 7, 8)

#### V4.1 — Provider card hierarchy
- **Goal:** One glance → name, specialty, distance. Demote provenance noise to a clear
  secondary tier. The badge row currently competes; give it explicit rank.
- **Files:** `src/main.js` (`buildCard`, `insuranceBadgesHtml`, `geoFlagHtml`),
  `carefind.html` (`<style>` card blocks).
- **Steps:** Establish a 3-tier card: **primary** (name, distance), **secondary**
  (specialty, address), **meta** (official-record/mapped/insurance as quieter chips
  with a consistent icon-left grammar). Reduce simultaneous color accents per card to
  ≤2. Make the whole card a single, obvious hit target with the save action clearly
  secondary.
- **Gate:** Documented badge-rank spec in design-system doc; heuristic scannability
  checklist committed; visual-regression approved; a11y names preserved (axe 0).
- **Commit:** `V4.1: 3-tier provider card hierarchy; quiet, ranked meta badges`

#### V4.2 — Insurance filter & coverage drawer polish
- **Goal:** Keep the (excellent, honest) verified/estimated model but reduce visual
  load; make the confidence system legible at a glance.
- **Files:** `src/main.js` (`renderInsuranceFilter`, `coverageHtml`), `carefind.html`.
- **Steps:** Tighten the mode toggle + legend into one compact, well-spaced unit;
  give confidence dots a consistent key; collapse long group lists with a "show more";
  align the drawer coverage list to the new type/spacing tokens.
- **Gate:** Visual-regression approved both themes; trust copy unchanged (verify against
  PLAN.md A2/A3 invariants); axe 0.
- **Commit:** `V4.2: compact, legible insurance filter + coverage drawer`

---

### PHASE V5 — Motion & signature moments (Categories 1, 6)

#### V5.1 — View Transitions + shared-element drawer
- **Goal:** Navigation feels continuous. Tab switches, list↔map, and card→drawer use
  the View Transitions API with a shared-element morph; everything stays 60fps and
  reduced-motion-safe.
- **Files:** `src/main.js` (`switchTab`, `setView`, `openDetail`/`closeDetail`),
  `carefind.html` (`view-transition-name` on the active card/drawer header + CSS).
- **Steps:** Wrap state swaps in `document.startViewTransition` (feature-detected;
  graceful fallback to current transitions). Give the active card avatar/name and the
  drawer header a shared `view-transition-name` for a morph. Verify reduced-motion
  disables the animation but not the navigation.
- **Gate:** Profiled 60fps; CLS≈0 across transitions; reduced-motion test green;
  fallback verified on a non-supporting engine in Playwright.
- **Commit:** `V5.1: View Transitions for tabs/list-map/card-drawer with shared element`

#### V5.2 — Brand the map + one signature moment
- **Goal:** The map should look like CareFind, and the app should have one moment of
  genuine craft that's memorable without being gimmicky.
- **Files:** `src/main.js` (tile/marker theming), `carefind.html`.
- **Steps:** Custom marker design tied to specialty color with a refined active state;
  brand-tinted tiles per theme; a subtle, tasteful entrance for results landing on the
  map (e.g. pins settle in sequence, reduced-motion-safe). Pick **one** signature: a
  refined search-to-results choreography, or a distinctive provider-card detail.
- **Gate:** Committed before/after; the signature documented in the design-principles
  doc; reduced-motion parity; 60fps.
- **Commit:** `V5.2: brand-themed map, refined markers, one signature moment`

---

### PHASE V6 — Accessibility-as-craft & interaction finish (Categories 8, 10)

#### V6.1 — Inline validation + interaction polish
- **Goal:** No error depends on a transient toast/shake alone; every field gives inline,
  persistent, accessible feedback; all targets ≥44px on touch.
- **Files:** `src/main.js` (`handleSearch` validation, field markup), `carefind.html`.
- **Steps:** Add inline `aria-describedby` error text under ZIP/NPI/city fields
  (keep the shake as reinforcement); ensure 44px touch targets for chips, save button,
  loc button, toggles; add hover/active/focus parity to every interactive element in
  both themes.
- **Gate:** axe 0 in both themes, all states; touch-target audit ≥44px; keyboard golden
  journey green; error states have persistent inline text.
- **Commit:** `V6.1: inline field validation, 44px targets, full state parity`

---

### PHASE V7 — Proof & visual regression (all categories) + final re-audit

#### V7.1 — Visual regression harness
- **Goal:** Lock the redesign so it can't silently regress. Screenshot every key
  surface × theme × breakpoint.
- **Files:** `tests-e2e/visual.spec.js` (Playwright `toHaveScreenshot`),
  `playwright.config.js`, `.github/workflows/ci.yml`.
- **Steps:** Snapshot welcome, results (seeded fixture), drawer, insurance filter,
  empty/error/backend-required/saved-empty — each at 360/768/1024/1440 in light + dark.
  Seed provider data from a committed fixture so screenshots are deterministic (reuse the
  injection approach: a fixture loader, not live NPPES).
- **Gate:** Visual suite green and required in CI; updating a baseline requires an
  explicit committed `--update-snapshots` diff review.
- **Commit:** `V7.1: Playwright visual-regression across themes and breakpoints`

#### V7.2 — Lighthouse, design-system doc, final re-audit
- **Goal:** Prove the bar and record it.
- **Files:** `docs/design-system.md`, `docs/design-principles.md`,
  `docs/design-audit.md` (the re-audit), `.github/workflows/ci.yml` (Lighthouse CI).
- **Steps:** Finish the design-system + principles docs (tokens, components, motion,
  voice); run Lighthouse (Perf/A11y/BP/SEO ≥98, reuse PLAN.md gate); re-run every gate
  in §2 and record pass/fail in `docs/design-audit.md`.
- **Gate:** **All ten gates in §2 pass.** That recorded re-audit *is* the 10/10.
- **Commit:** `V7.2: design-system + principles docs, Lighthouse gate, final visual re-audit`

---

## 4. Per-task verification protocol

Before every PR:
1. `npm run build` reproduces the bundle; Vitest + Playwright (incl. visual + a11y) green.
2. `node scripts/lint-tokens.mjs` and the contrast audit pass.
3. The change renders correctly in **both themes** at all four breakpoints (attach
   screenshots or rely on the visual suite).
4. **Trust invariant intact** — no presentation change weakens or contradicts a
   verified/estimated claim (cross-check PLAN.md §1.1, A2, A3).
5. Reduced-motion path verified for any motion change.

## 5. Definition of Done (whole program)

Complete when `docs/design-audit.md` shows **every §2 gate green**, the visual-regression
suite is required and passing in CI in both themes at all breakpoints, token-lint and
contrast gates are enforced, and a fresh reviewer cannot point to a magic number, a
cramped seam, a missing dark-mode state, or an un-ranked card — all at **$0**, with the
single-file deploy and every trust invariant intact.

## 6. Sequencing & urgency

V0 is the long pole and unblocks everything — **do it first and completely**; resist
shipping a visible win (dark mode, a signature) before the token layer exists, or you'll
pay for it five times over. After V0, V1 (dark mode) is the highest-visibility,
highest-credibility win and should land immediately. V3.2 (mobile map + header) closes
the weakest category. V5 adds the craft that separates 9 from 10. Ship each phase as it
greens; do not batch.
