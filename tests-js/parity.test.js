// T1.5 (JS half): the frontend buildProviders() must produce the same structural
// fields as the backend normalize() for a shared golden NPPES record. The Python
// half asserts the identical fixture in tests/test_api.py::test_normalize_matches_golden,
// so renaming/changing a field on EITHER side fails CI. Phone/fax are excluded
// (the backend returns them raw; the frontend formats them downstream).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import logic from '../carefind.logic.js';

// Vitest runs from the repo root; the jsdom env makes import.meta.url an http URL,
// so resolve the shared fixture from cwd instead.
const golden = JSON.parse(readFileSync(join(process.cwd(), 'tests', 'fixtures', 'normalize_golden.json'), 'utf-8'));

describe('buildProviders <-> backend normalize() parity (golden fixture)', () => {
  it('produces the shared structural fields field-for-field', () => {
    const [out] = logic.buildProviders([golden.record]);
    for (const [key, val] of Object.entries(golden.expected)) {
      expect(out[key], `buildProviders() field "${key}" drifted from the golden fixture`).toStrictEqual(val);
    }
  });
});
