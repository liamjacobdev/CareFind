// Unit tests for the extracted pure frontend logic (carefind.logic.js).
// These are the Node-side guard the page lacked; they also lock the
// buildProviders <-> backend normalize() contract (T1.5) and the NPPES query
// construction (mirrors tests/test_nppes.py).
import { describe, it, expect } from 'vitest';
import logic from '../carefind.logic.js';

const {
  esc, cssEsc, haversine, formatPhone, toTitleCase, hashStr,
  buildNpiParams, buildProviders, adaptBackendProvider, PALETTE, TAXONOMY_MAP,
  coverageStatus, fmtDate,
} = logic;

describe('fmtDate — provenance "checked <date>" (A3)', () => {
  it('formats epoch seconds as a stable UTC date', () => {
    // 1700000000 = 2023-11-14T22:13:20Z
    expect(fmtDate(1700000000)).toBe('Nov 14, 2023');
  });
  it('returns empty for a missing/invalid timestamp', () => {
    expect(fmtDate(null)).toBe('');
    expect(fmtDate(undefined)).toBe('');
    expect(fmtDate(NaN)).toBe('');
  });
});

describe('coverageStatus — payer vs plan level (A2)', () => {
  it('labels a plan-level verified hit "Confirmed"', () => {
    expect(coverageStatus({ value: true, confidence: 'verified', level: 'plan' }))
      .toEqual({ cls: 'yes', text: 'Confirmed' });
  });
  it('NEVER labels a payer-level hit "Confirmed" — it is a network listing', () => {
    const s = coverageStatus({ value: true, confidence: 'verified', level: 'payer' });
    expect(s.text).not.toBe('Confirmed');
    expect(s).toEqual({ cls: 'innet', text: 'In-network' });
  });
  it('defaults missing level to payer (never over-claims as Confirmed)', () => {
    expect(coverageStatus({ value: true, confidence: 'verified' }).text).not.toBe('Confirmed');
  });
  it('verified false reads as not-enrolled (plan) vs not-listed (payer)', () => {
    expect(coverageStatus({ value: false, confidence: 'verified', level: 'plan' }).text).toBe('Not enrolled');
    expect(coverageStatus({ value: false, confidence: 'verified', level: 'payer' }).text).toBe('Not listed');
  });
  it('verified unknown (null) is never a yes', () => {
    expect(coverageStatus({ value: null, confidence: 'verified', level: 'payer' }))
      .toEqual({ cls: 'unknown', text: 'Unverified' });
  });
  it('an estimate is always "Likely", never Confirmed/In-network', () => {
    const s = coverageStatus({ value: true, confidence: 'estimated', level: 'payer' });
    expect(s).toEqual({ cls: 'likely', text: 'Likely · confirm' });
    expect(coverageStatus({ value: null, confidence: 'estimated' }).text).toBe('Unverified');
  });
  it('a non-filterable national estimate reads as area context, not a match (A4)', () => {
    const s = coverageStatus({ value: true, confidence: 'estimated' }, false);
    expect(s).toEqual({ cls: 'likely', text: 'Operates in your area' });
    // A filterable (regional) estimate keeps the "likely · confirm" wording.
    expect(coverageStatus({ value: true, confidence: 'estimated' }, true).text).toBe('Likely · confirm');
  });
  it('returns null when there is nothing to show', () => {
    expect(coverageStatus(null)).toBeNull();
    expect(coverageStatus(undefined)).toBeNull();
  });
});

describe('esc — HTML escaping (XSS)', () => {
  it('neutralizes an <img onerror> payload', () => {
    expect(esc('<img src=x onerror="alert(1)">'))
      .toBe('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  });
  it('escapes the five dangerous chars', () => {
    expect(esc(`&<>"'`)).toBe('&amp;&lt;&gt;&quot;&#39;');
  });
  it('preserves the literal 0 but maps other falsy to empty', () => {
    expect(esc(0)).toBe('0');
    expect(esc('')).toBe('');
    expect(esc(null)).toBe('');
    expect(esc(undefined)).toBe('');
  });
});

describe('cssEsc — attribute-selector escaping', () => {
  it('escapes quotes and backslashes', () => {
    expect(cssEsc('a"b\\c')).toBe('a\\"b\\\\c');
  });
  it('leaves a normal NPI untouched', () => {
    expect(cssEsc('1003000126')).toBe('1003000126');
  });
});

describe('haversine — great-circle miles', () => {
  it('is ~0 for identical points', () => {
    expect(haversine([30.0, -86.0], [30.0, -86.0])).toBeCloseTo(0, 6);
  });
  it('matches a known short distance', () => {
    // ~0.91 mi between these two Crestview, FL points.
    expect(haversine([30.77, -86.58], [30.76, -86.57])).toBeCloseTo(0.91, 1);
  });
  it('matches a known long distance (NYC <-> LA ~2445 mi)', () => {
    expect(haversine([40.7128, -74.006], [34.0522, -118.2437])).toBeCloseTo(2445, -2);
  });
});

describe('formatPhone', () => {
  it('formats a 10-digit number', () => {
    expect(formatPhone('8005551234')).toBe('(800) 555-1234');
  });
  it('strips a leading country 1 from an 11-digit number', () => {
    expect(formatPhone('18005551234')).toBe('(800) 555-1234');
  });
  it('passes through anything it cannot format', () => {
    expect(formatPhone('555')).toBe('555');
    expect(formatPhone('')).toBe('');
  });
});

describe('toTitleCase', () => {
  it('title-cases words and upper-cases credential suffixes', () => {
    expect(toTitleCase('JANE DOE md')).toBe('Jane Doe MD');
    expect(toTitleCase('gulf coast llc')).toBe('Gulf Coast LLC');
  });
  it('returns empty for falsy input', () => {
    expect(toTitleCase('')).toBe('');
  });
});

describe('hashStr', () => {
  it('is deterministic and unsigned', () => {
    expect(hashStr('Cardiology')).toBe(hashStr('Cardiology'));
    expect(hashStr('Cardiology')).toBeGreaterThanOrEqual(0);
  });
  it('indexes within the PALETTE', () => {
    expect(PALETTE[hashStr('Family Medicine') % PALETTE.length]).toMatch(/^#[0-9a-f]{6}$/);
  });
});

describe('buildNpiParams — NPPES query construction (mirrors test_nppes.py)', () => {
  const get = (f) => Object.fromEntries(buildNpiParams(f).entries());

  it('an NPI lookup shortcuts other params', () => {
    const p = get({ npi: '1003000126', zip: '32536', limit: 25 });
    expect(p.number).toBe('1003000126');
    expect(p.postal_code).toBeUndefined();
  });
  it('uses the exact ZIP for a small radius', () => {
    expect(get({ zip: '32536', radius: 10, limit: 25 }).postal_code).toBe('32536');
    expect(get({ zip: '32536', limit: 25 }).postal_code).toBe('32536');
  });
  it('widens to a 3-digit prefix for a large radius', () => {
    expect(get({ zip: '32536', radius: 25, limit: 25 }).postal_code).toBe('325*');
    expect(get({ zip: '32536', radius: 100, limit: 25 }).postal_code).toBe('325*');
  });
  it('splits a person name into wildcarded first/last', () => {
    const p = get({ name: 'John Smith', city: 'Crestview', st: 'FL', limit: 25 });
    expect(p.first_name).toBe('John*');
    expect(p.last_name).toBe('Smith*');
  });
  it('uses organization_name for an org-type search', () => {
    const p = get({ name: 'Gulf Coast', type: 'NPI-2', st: 'FL', limit: 25 });
    expect(p.organization_name).toBe('Gulf Coast*');
  });
  it('maps a specialty label to its taxonomy_description', () => {
    const p = get({ zip: '32536', specialty: 'Cardiology', limit: 25 });
    expect(p.taxonomy_description).toBe(TAXONOMY_MAP['Cardiology']);
    expect(p.taxonomy_description).toBe('Cardiovascular Disease');
  });
  it('sets address_purpose=LOCATION when location-scoped', () => {
    expect(get({ zip: '32536', limit: 25 }).address_purpose).toBe('LOCATION');
  });
});

describe('buildProviders — mirrors the backend normalize() shape', () => {
  const record = {
    number: 1003000126,
    enumeration_type: 'NPI-1',
    basic: { first_name: 'jane', last_name: 'doe', credential: 'M.D.', status: 'A',
             gender: 'F', enumeration_date: '2010-01-01', last_updated: '2020-01-01' },
    addresses: [
      { address_purpose: 'LOCATION', address_1: '1 main st', city: 'crestview',
        state: 'FL', postal_code: '325361234', telephone_number: '850-555-1234', fax_number: '8505556789' },
      { address_purpose: 'MAILING', address_1: 'po box 9', city: 'crestview', state: 'FL', postal_code: '32536' },
    ],
    taxonomies: [{ desc: 'Cardiovascular Disease', code: '207RC0000X', primary: true, state: 'FL', license: '123' }],
  };

  it('normalizes a person record field-for-field', () => {
    const [p] = buildProviders([record]);
    expect(p.npi).toBe('1003000126');
    // Only the trailing period is stripped from the credential ('M.D.' -> 'M.D').
    expect(p.name).toBe('Jane Doe, M.D');
    expect(p.isOrg).toBe(false);
    expect(p.specialty).toBe('Cardiovascular Disease');
    expect(p.address1).toBe('1 Main St');
    expect(p.city).toBe('Crestview');
    expect(p.stateAb).toBe('FL');
    expect(p.postalCode).toBe('32536');         // truncated to 5
    expect(p.phone).toBe('(850) 555-1234');
    expect(p.fax).toBe('(850) 555-6789');
    expect(p.gender).toBe('Female');
    expect(p.status).toBe('Active');
    expect(p.mailingAddress).toContain('Po Box 9');
    expect(p.lat).toBeNull();
    expect(p.geocoded).toBe(false);
    expect(PALETTE).toContain(p.color);
  });

  it('handles an organization record and missing fields', () => {
    const [p] = buildProviders([{ number: 1, enumeration_type: 'NPI-2',
      basic: { organization_name: 'gulf coast health llc' }, addresses: [], taxonomies: [] }]);
    expect(p.isOrg).toBe(true);
    expect(p.name).toBe('Gulf Coast Health LLC');
    expect(p.specialty).toBe('Healthcare Provider');
    expect(p.initials).toBe('GC');
    expect(p.fullAddress).toBe('');
  });
});

describe('adaptBackendProvider', () => {
  const backendShape = {
    npi: '1003000126', name: 'Jane Doe', isOrg: false, specialty: 'Cardiology',
    address1: '1 Main St', city: 'Crestview', stateAb: 'FL', postalCode: '32536',
    phone: '8505551234', insurance: { medicare: { value: true, confidence: 'verified' } },
    lat: 30.77, lng: -86.58,
  };

  it('computes distance from a supplied center', () => {
    const p = adaptBackendProvider(backendShape, [30.76, -86.57]);
    expect(p.geocoded).toBe(true);
    expect(p.distance).toBeCloseTo(0.91, 1);
    expect(p.phone).toBe('(850) 555-1234');
    expect(p.insurance.medicare.confidence).toBe('verified');
  });

  it('leaves distance null without a center or coords', () => {
    expect(adaptBackendProvider(backendShape, null).distance).toBeNull();
    const noCoords = adaptBackendProvider({ ...backendShape, lat: null, lng: null }, [30, -86]);
    expect(noCoords.distance).toBeNull();
    expect(noCoords.geocoded).toBe(false);
  });

  it('derives initials for a person and an org', () => {
    expect(adaptBackendProvider({ ...backendShape, name: 'Jane Doe' }, null).initials).toBe('JD');
    expect(adaptBackendProvider({ npi: '1', name: 'Gulf Coast Clinic', isOrg: true }, null).initials).toBe('GC');
  });
});
