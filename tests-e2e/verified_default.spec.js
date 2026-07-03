// T3.4: verified-by-default. The insurance filter must offer only Confirmed plans
// by default; estimated payers appear solely after opting in via "Include estimated"
// and are always labeled "likely", never "Confirmed".
import { test, expect } from '@playwright/test';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'innetwork.html')).href;

const MED = {
  id: 'medicare',
  label: 'Medicare (Original)',
  category: 'medicare',
  payer: 'medicare',
  confidence: 'verified',
  kind: 'government',
};
const AETNA = {
  id: 'aetna',
  label: 'Aetna',
  category: 'commercial',
  payer: 'aetna',
  confidence: 'estimated',
  kind: 'commercial',
};
const PLANS = {
  plans: [MED, AETNA],
  categories: [
    { id: 'medicare', label: 'Medicare', plans: [MED] },
    { id: 'commercial', label: 'Commercial', plans: [AETNA] },
  ],
};

test('estimated payers are hidden until "Include estimated" is toggled', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({ json: {} }));
  await page.route('**/healthz', (r) => r.fulfill({ json: { ok: true } }));
  await page.route('**/api/insurance/plans', (r) => r.fulfill({ json: PLANS }));
  await page.goto(PAGE);

  const chips = page.locator('#insurance-filter .ins-chip');
  // Default (verified-only): Medicare present, the estimated Aetna chip is not.
  await expect(page.locator('#insurance-filter .ins-chip[data-plan="medicare"]')).toBeVisible();
  await expect(page.locator('#insurance-filter .ins-chip[data-plan="aetna"]')).toHaveCount(0);
  await expect(page.locator('#insurance-filter .conf-dot.estimated')).toHaveCount(0);

  // Opt in: the estimated payer now appears (labeled estimated, never Confirmed).
  await page.locator('#insurance-filter [data-action="ins-mode"][data-mode="any"]').click();
  await expect(page.locator('#insurance-filter .ins-chip[data-plan="aetna"]')).toBeVisible();
  await expect(chips).toHaveCount(2);

  // Back to verified-only hides it again.
  await page.locator('#insurance-filter [data-action="ins-mode"][data-mode="verified"]').click();
  await expect(page.locator('#insurance-filter .ins-chip[data-plan="aetna"]')).toHaveCount(0);
});
