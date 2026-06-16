# Contributing to CareFind

Thanks for helping make provider + insurance search more trustworthy. The guiding
principle: **never show a user something we can't stand behind.** A silent failure or
a misleading "Confirmed" is worse than an honest "we don't know."

## Ground rules

- **Verified vs. estimated is sacred.** An "estimated" answer must never render as
  *Confirmed*. New data sources are wired so a payer graduates Estimated → Confirmed
  automatically via its catalog `id` (`app/catalog.py`) — the universal join key.
- **No fabricated data.** A source answers True / False / **None (unknown)**; unknown
  is never turned into a yes or a no.
- **Don't ship what you can't verify.** Every change needs a test or a documented
  manual check.

### The trust rules are executable

These promises are not just prose — they are enforced by `tests/test_trust_rules.py`,
which fails CI if any code path can overclaim. Read that file before touching the
insurance layer; if you add a branch, extend the relevant rule. The invariants:

1. **Presence ≠ Confirmed.** A FHIR directory hit is True only for an *active*
   `PractitionerRole` with a resolvable network link (`healthcareService` doesn't
   count). Listed-but-unconfirmable is `None`, not a yes.
2. **Unknown stays unknown.** A cached/unknown answer maps to `None`, never to True
   and never to a "no".
3. **Estimates can't masquerade.** The estimated tier only ever emits True/None (never
   False), always carries `confidence: "estimated"`, and never carries provenance.
4. **Every verified True is traceable.** It always carries a non-empty `source_url`
   and `fetched_at` (the verify-link provenance).
5. **Payer ≠ plan.** A payer-directory listing (`level: "payer"`) is rendered as
   "In-network", never "Confirmed" — only a single-program plan (`level: "plan"`) is.
6. **Non-discriminating estimates don't filter.** A national "operates everywhere"
   estimate is context, not a filter, so it can't imply provider-specific acceptance.

## Dev setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                  # backend tests

npm install                # frontend tooling
npm test                   # Vitest unit tests (carefind.logic.js), enforces coverage
npm run test:e2e           # Playwright smoke (needs: npx playwright install chromium)
```

Run the app with `python -m uvicorn app.main:app --port 8000` and open
<http://localhost:8000>.

## Tests

- **Backend** (`tests/`, pytest): keep it green; new behavior needs coverage.
- **Frontend logic** (`tests-js/`, Vitest): pure functions live in `carefind.logic.js`
  — the single source of truth shared with the page. Coverage threshold is enforced.
- **Cross-language parity** (`tests/fixtures/normalize_golden.json`): the backend
  `normalize()` and the frontend `buildProviders()` must agree field-for-field. If you
  add a field to one, add it to the other and update the fixture.
- **E2E** (`tests-e2e/`, Playwright): the core flows over a mocked backend.

CI (`.github/workflows/ci.yml`) runs pytest, Vitest (with coverage), and Playwright on
every push and PR. Please keep all three green.

## Commits

One focused change per commit, with a message explaining the *why*. Keep the test
suite green at every commit.

## License

By contributing you agree your contributions are licensed under the [MIT License](LICENSE).
