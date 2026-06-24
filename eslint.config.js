// ESLint flat config (Category 3): real-bug + hygiene linting for the frontend JS.
// The generated bundle and vendored dirs are ignored; each area gets the right globals.
import js from '@eslint/js';
import globals from 'globals';

export default [
  {
    ignores: [
      'carefind.bundle.js',
      'node_modules/**',
      'coverage/**',
      'test-results/**',
      'playwright-report/**',
      '**/.venv/**',
    ],
  },
  js.configs.recommended,
  {
    // Project-wide rule tweaks: a leading `_` marks an intentionally-unused arg/var/catch,
    // and an empty `catch {}` (ignore-and-move-on) is a deliberate pattern here.
    rules: {
      'no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
  {
    // Browser app code: the page bundle source, the injected config, the pure logic,
    // and the tiny pre-paint theme init (classic <head> script, not a module).
    files: ['src/**/*.js', 'carefind.logic.js', 'carefind.config.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, L: 'readonly' }, // L = Leaflet, loaded from a CDN at runtime
    },
  },
  {
    files: ['carefind.theme.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'script', globals: globals.browser },
  },
  {
    // Build + tooling config (Node, ESM).
    files: ['build.mjs', '*.config.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: globals.node },
  },
  {
    // Tests (Vitest + Playwright import their APIs; run under Node). Playwright specs
    // also run `page.evaluate` callbacks in the browser, so allow browser globals too.
    files: ['tests-js/**/*.js', 'tests-e2e/**/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: { ...globals.node, ...globals.browser } },
  },
];
