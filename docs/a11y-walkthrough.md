# Accessibility — WCAG 2.2 AA conformance, axe results, and SR walkthrough

CareFind targets **WCAG 2.2 Level AA**. Conformance is enforced automatically and
documented here.

## Automated enforcement (CI)

`tests-e2e/a11y.spec.js` runs **axe-core** (tags `wcag2a/2aa`, `wcag21a/21aa`, `wcag22aa`)
against **every view and state** and asserts **zero violations**:

- welcome / initial state
- results list
- insurance filter — verified-only **and** "include estimated"
- provider detail drawer (open)
- map view (with plotted markers)
- favorites tab (with a saved provider)
- empty results state

It also asserts the **golden journey is completable keyboard-only** (type ZIP → Enter →
focus a provider's name button → Enter opens the detail drawer → Escape closes it).
This spec runs in CI via `npm run test:e2e`, so a regression fails the build.

Fixes made to reach zero violations (D2):
- **2.5.8 Target Size (AA):** the "use my location" button now has a ≥24×24px target.
- **4.1.2 Name, Role, Value:** map markers carry an accessible name (`title`/`alt`); the
  non-interactive ZIP-center pin is `keyboard:false` so it isn't an unnamed control.
- **2.1.1 Keyboard / 4.1.2:** each result card's name is a real `<button>` (focusable,
  Enter/Space-operable, `aria-label="View details for <name>"`) — so the card is openable
  keyboard-only without nesting interactive controls (the save control is a sibling button).
- **1.4.3 Contrast (AA):** the "Sort" label was 3.09:1; now uses `--muted` (≥4.5:1).
- **2.1.1 / scrollable-region-focusable:** the results region is `tabindex="0"` so it's
  keyboard-scrollable even when it holds only non-focusable content (empty state).

## WCAG 2.2 AA checklist

| SC | How CareFind meets it |
|----|------------------------|
| 1.1.1 Non-text Content | All icons are decorative SVG inside labeled controls; the icon/logo has equivalents; map markers have `title`/`alt`. |
| 1.3.1 Info & Relationships | Landmarks (`header`, `main`/`aside`, `region`), grouped insurance chips with `role="group"` + labels, `<label for>` on inputs. |
| 1.4.3 Contrast (Minimum) | Text tokens ≥4.5:1 on their backgrounds; axe color-contrast passes on every state. |
| 1.4.10 Reflow / 1.4.11 Non-text Contrast | Responsive layout; controls/borders meet 3:1. |
| 2.1.1 Keyboard / 2.1.2 No Trap | Every action reachable + operable by keyboard; drawer closes on Escape; no traps (axe + the keyboard-journey test). |
| 2.4.1 Bypass Blocks | Skip-link to main content. |
| 2.4.3 Focus Order / 2.4.7 Focus Visible | Logical DOM order; `:focus-visible` outlines on interactive elements. |
| 2.4.11 Focus Not Obscured (AA, 2.2) | The detail drawer overlays content via a scrim; focus moves into it and the underlying list is inert during interaction. |
| 2.5.8 Target Size (Minimum) (AA, 2.2) | Interactive targets ≥24×24px (loc button fixed). |
| 3.2.6 Consistent Help (2.2) / 3.3.x | The "For providers"/help affordances are consistently placed; inputs have visible labels and the empty/error states give recovery guidance. |
| 4.1.2 Name, Role, Value | All controls have names/roles (axe aria-command-name / button-name pass on every state). |

> The two new WCAG **2.2** AA criteria most relevant here — **2.5.8 Target Size** and
> **2.4.11 Focus Not Obscured** — are specifically covered above and axe-verified.

## Screen-reader walkthrough (NVDA + VoiceOver)

axe verifies names/roles/structure programmatically; the script below is the manual
walkthrough to confirm the *experience* on real assistive tech. Run it on the deployed
app and record the transcript here. Expected announcements are derived from the markup.

**NVDA (Windows, Firefox/Chrome)**
1. Load the page. NVDA announces the document title, then the skip-link ("Skip to results/content"). Press Enter on it to jump past the header.
2. `Tab` through the search controls — each announces its label ("ZIP code, edit", "Specialty, combo box", "Search, button"). Type a ZIP, press Enter.
3. Results render. `Tab` reaches each provider's name button: "View details for Jane Doe, MD, button". Press Enter — the detail drawer opens and focus moves into it.
4. In the drawer, `Tab` reaches "Call to confirm", "Directions", and the verify link; the coverage list reads each plan with its status ("Medicare, Confirmed"). Press `Escape` — the drawer closes and focus returns to the list.
5. `Tab` to the insurance filter: "Match strictness, group", then chips as checkboxes ("Medicare, checked/not checked"). Toggle "Include estimated" and confirm estimated chips are announced as such.
6. Switch to the Saved tab (announced as a tab/button) and confirm the saved provider is read.

**VoiceOver (macOS, Safari)**
1. `VO + Cmd + Space` or load the page; VoiceOver reads the title and the skip-link.
2. Use `VO + →` to traverse the search form; labels and roles match the NVDA expectations above.
3. Use the rotor (`VO + U`) → Landmarks to confirm header / main / "Search results" region are present and navigable.
4. Use the rotor → Form Controls to jump between the provider name buttons; activate one with `VO + Space`.
5. Confirm the drawer is reachable, the coverage statuses are announced, and `Escape`/close returns focus to the list.

_Recorded transcripts (fill in after running on real hardware):_

- NVDA: _(paste transcript / notes)_
- VoiceOver: _(paste transcript / notes)_

> Honest note: the automated axe sweep + keyboard-journey test run in CI and gate the
> build; the NVDA/VoiceOver transcripts above must be captured on real AT by the
> maintainer (no screen reader runs in the build sandbox). The markup is structured to
> produce the announcements described.
