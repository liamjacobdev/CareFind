import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests-js/**/*.test.js'],
    coverage: {
      provider: 'v8',
      include: ['carefind.logic.js'],
      reporter: ['text', 'json-summary'],
      // The extracted pure logic is the contract that mirrors the backend; hold it to
      // the 10/10 bar (E1) so a future change can't quietly drop its coverage.
      thresholds: { lines: 90, functions: 90, statements: 90, branches: 90 },
    },
  },
});
