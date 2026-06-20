import { defineConfig, devices } from '@playwright/test';

// The smoke test loads carefind.html over file:// and mocks every /api/* call, so no
// server is needed (and it doubles as a check that the file:// path still works).
const VISUAL = /visual\.spec\.js/;

export default defineConfig({
  testDir: 'tests-e2e',
  fullyParallel: true,
  reporter: 'list',
  use: { headless: true },
  // Visual baselines: a small per-pixel color tolerance absorbs anti-aliasing noise;
  // the spec sets maxDiffPixelRatio per shot.
  expect: { toHaveScreenshot: { threshold: 0.2 } },
  projects: [
    // Cross-engine (E1): the golden journey + axe sweep run on all three engines.
    // The visual suite is excluded here — it runs once, on chromium, in `visual`.
    { name: 'chromium', use: { ...devices['Desktop Chrome'] }, testIgnore: VISUAL },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] }, testIgnore: VISUAL },
    { name: 'webkit', use: { ...devices['Desktop Safari'] }, testIgnore: VISUAL },
    // Visual-regression (V7): one engine for stable pixel baselines. Baselines are
    // OS-specific; generate + run them on the matching OS (see CI `visual` job).
    { name: 'visual', use: { ...devices['Desktop Chrome'] }, testMatch: VISUAL },
  ],
});
