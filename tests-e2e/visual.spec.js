// Visual-regression harness (V7): pixel baselines for the premium redesign across
// BOTH themes and key breakpoints, so a styling regression can't slip in silently.
//
// Determinism: the page loads over file:// with every /api/* call mocked (providers
// carry coordinates, so no live geocoding), the map tile region is masked (CDN tiles
// are non-deterministic), web fonts are awaited, and Playwright's toHaveScreenshot
// disables CSS animations — so the only thing under test is layout, type, and color.
//
// Baselines are OS-specific (Playwright suffixes the platform). They are generated and
// run on the matching OS — see the `visual` job in .github/workflows/ci.yml. Regenerate
// with `npm run test:visual:update` after an intentional visual change.
import { test, expect } from '@playwright/test';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'carefind.html')).href;

const PLAN_MEDICARE = {
  id: 'medicare',
  label: 'Medicare (Original)',
  category: 'medicare',
  payer: 'medicare',
  confidence: 'verified',
  kind: 'government',
  level: 'plan',
  filterable: true,
};
const PLAN_AETNA = {
  id: 'aetna',
  label: 'Aetna',
  category: 'commercial',
  payer: 'aetna',
  confidence: 'verified',
  kind: 'commercial',
  level: 'payer',
  filterable: true,
};
const PLANS = {
  plans: [PLAN_MEDICARE, PLAN_AETNA],
  categories: [
    { id: 'medicare', label: 'Medicare', plans: [PLAN_MEDICARE] },
    { id: 'commercial', label: 'Commercial', plans: [PLAN_AETNA] },
  ],
};

const MEDICARE_OK = {
  value: true,
  confidence: 'verified',
  level: 'plan',
  source: 'medicare',
  source_url: 'https://www.medicare.gov/care-compare/',
  fetched_at: '2026-06-10',
};
const AETNA_NET = {
  value: true,
  confidence: 'verified',
  level: 'payer',
  source: 'aetna_planet',
  source_url: 'https://www.aetna.com/dsepe/',
  fetched_at: '2026-06-09',
};

function provider(npi, name, specialty, address1, lat, lng, insurance) {
  return {
    npi,
    name,
    isOrg: /associates|clinic|center|practice/i.test(name),
    specialty,
    taxonomies: [{ desc: specialty, code: '207RC0000X', primary: true, license: '', state: 'NY' }],
    address1,
    city: 'New York',
    stateAb: 'NY',
    postalCode: '10001',
    fullAddress: `${address1}, New York, NY 10001`,
    mailingAddress: '',
    phone: '2125550142',
    fax: '',
    gender: '',
    soleProprietor: '',
    credential: 'MD',
    status: 'Active',
    enumerationDate: '2009-05-12',
    lastUpdated: '2025-11-02',
    insurance,
    lat,
    lng,
  };
}

const SEARCH = {
  count: 3,
  total: 3,
  truncated: false,
  pool_capped: false,
  plans: PLANS.plans,
  providers: [
    provider('1003000126', 'Sarah Chen, MD', 'Cardiology', '275 7th Ave', 40.7449, -73.9925, {
      medicare: MEDICARE_OK,
      aetna: AETNA_NET,
    }),
    provider('1013000125', 'Marcus Williams, MD', 'Interventional Cardiology', '525 E 68th St', 40.7649, -73.9541, {
      aetna: AETNA_NET,
    }),
    provider(
      '1023000124',
      'Gulf Coast Heart Associates',
      'Cardiovascular Disease',
      '1090 Amsterdam Ave',
      40.8021,
      -73.9627,
      {
        medicare: MEDICARE_OK,
      },
    ),
  ],
};

async function mockApi(page) {
  await page.route('**/api/**', (r) => r.fulfill({ json: {} }));
  await page.route('**/healthz', (r) => r.fulfill({ json: { ok: true } }));
  await page.route('**/api/insurance/plans', (r) => r.fulfill({ json: PLANS }));
  await page.route('**/api/providers/search**', (r) => r.fulfill({ json: SEARCH }));
}

// Load the page in a given theme and wait until it's visually settled.
async function open(page, theme) {
  await mockApi(page);
  await page.addInitScript((t) => {
    try {
      localStorage.setItem('carefind_theme', t);
    } catch (e) {
      void e;
    }
  }, theme);
  await page.goto(PAGE);
  await expect(page.locator('#search-btn')).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

async function runSearch(page) {
  await page.fill('#zip-input', '10001');
  await page.click('#search-btn');
  // Wait for REAL results, not the skeletons (which also use .provider-card): a provider
  // name appears and the button has returned from its "Searching…" loading label.
  await expect(page.locator('#results-list')).toContainText('Sarah Chen');
  await expect(page.locator('#search-btn')).toContainText('Search providers');
  await page.evaluate(() => document.fonts.ready);
}

const SNAP = { maxDiffPixelRatio: 0.02, animations: 'disabled' };
const DESKTOP = { width: 1280, height: 900 };
const MOBILE = { width: 390, height: 844 };

// Reveal the result cards: in the fixed-height split layout the search panel pushes the
// list below the fold, so collapse it before snapshotting the sidebar/list.
async function revealResults(page) {
  await page.locator('#search-panel').evaluate((el) => (el.style.display = 'none'));
}

for (const theme of ['light', 'dark']) {
  test.describe(`${theme} theme`, () => {
    test(`welcome — desktop (${theme})`, async ({ page }) => {
      await page.setViewportSize(DESKTOP);
      await open(page, theme);
      // Whole shell (search form, header, data note) with the map region masked out.
      await expect(page).toHaveScreenshot(`welcome-desktop-${theme}.png`, {
        ...SNAP,
        maskColor: '#0c1f1a',
        mask: [page.locator('#map-container')],
      });
    });

    test(`results list — desktop (${theme})`, async ({ page }) => {
      await page.setViewportSize(DESKTOP);
      await open(page, theme);
      await runSearch(page);
      await revealResults(page);
      // Component shot of the cards grid — map-free, so fully deterministic.
      await expect(page.locator('#sidebar')).toHaveScreenshot(`results-desktop-${theme}.png`, SNAP);
    });

    test(`detail drawer — desktop (${theme})`, async ({ page }) => {
      await page.setViewportSize(DESKTOP);
      await open(page, theme);
      await runSearch(page);
      await page.locator('#results-list .provider-card').first().click();
      await expect(page.locator('#detail-drawer')).toHaveClass(/open/);
      await page.waitForTimeout(450); // let the slide-in settle
      await expect(page.locator('#detail-drawer')).toHaveScreenshot(`drawer-desktop-${theme}.png`, SNAP);
    });

    test(`results list — mobile (${theme})`, async ({ page }) => {
      await page.setViewportSize(MOBILE);
      await open(page, theme);
      await runSearch(page);
      await revealResults(page);
      await expect(page.locator('#sidebar')).toHaveScreenshot(`results-mobile-${theme}.png`, SNAP);
    });
  });
}
