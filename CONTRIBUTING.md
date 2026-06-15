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
