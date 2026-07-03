import { defineConfig, devices } from '@playwright/test';

// The smoke test loads innetwork.html over file:// and mocks every /api/* call, so no
// server is needed (and it doubles as a check that the file:// path still works).
export default defineConfig({
  testDir: 'tests-e2e',
  fullyParallel: true,
  reporter: 'list',
  use: { headless: true },
  projects: [
    // Cross-engine (E1): the golden journey + axe sweep run on all three engines.
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
