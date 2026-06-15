import { defineConfig, devices } from '@playwright/test';

// The smoke test loads carefind.html over file:// and mocks every /api/* call, so no
// server is needed (and it doubles as a check that the file:// path still works).
export default defineConfig({
  testDir: 'tests-e2e',
  fullyParallel: true,
  reporter: 'list',
  use: { headless: true },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
