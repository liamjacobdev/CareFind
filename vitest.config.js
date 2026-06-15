import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests-js/**/*.test.js'],
    coverage: {
      provider: 'v8',
      include: ['carefind.logic.js'],
      reporter: ['text', 'json-summary'],
      // The extracted pure logic is the contract that mirrors the backend; hold it
      // to a real bar so a future change can't quietly drop its coverage.
      thresholds: { lines: 80, functions: 80, statements: 80, branches: 75 },
    },
  },
});
