// T5.1: when the backend isn't reachable and there's no working standalone path,
// the page must say so plainly (the CMS registry can't be queried from a browser),
// not advertise a search it can't deliver.
import { test, expect } from '@playwright/test';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'innetwork.html')).href;

test('shows an honest "start the backend" state when the backend is unreachable', async ({ page }) => {
  // Abort every backend call so it behaves exactly like "backend not running"
  // (public proxies are off by default, so there is no working standalone path).
  await page.route('**/healthz', (r) => r.abort());
  await page.route('**/api/**', (r) => r.abort());

  await page.goto(PAGE);
  await page.fill('#zip-input', '32536');
  await page.click('#search-btn');

  const list = page.locator('#results-list');
  await expect(list).toContainText('Start the InNetwork backend');
  await expect(list).toContainText('uvicorn app.main:app');
  // It must NOT pretend a search succeeded.
  await expect(list.locator('.provider-card')).toHaveCount(0);
});
