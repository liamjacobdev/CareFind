// $0 load test (D4). Run against a deployed instance to confirm the p95 latency target
// and that the breaker + caches hold under load. k6 is a free, single-binary tool.
//
//   BASE=https://api.yourdomain.com k6 run loadtest/k6_search.js
//
// Gate: p95 < 800ms and <1% failed requests. Tune VUs/duration to your box. Because the
// NPPES/geocode results are cached (short TTL) and FHIR results are cached per (payer,
// NPI), a realistic mix of repeated searches mostly hits warm caches — the breaker keeps
// p95 bounded even if an upstream degrades.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE || 'http://localhost:8000';
const ZIPS = ['10001', '90210', '60601', '33139', '94103', '02134', '98101', '78701'];
const SPECS = ['Cardiology', 'Dermatology', 'Family Medicine', 'Pediatrics'];

export const options = {
  scenarios: {
    steady: { executor: 'ramping-vus', startVUs: 0,
      stages: [{ duration: '30s', target: 25 }, { duration: '2m', target: 25 }, { duration: '30s', target: 0 }] },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],   // <1% errors (the chaos goal: no 5xx pile-up)
    http_req_duration: ['p(95)<800'], // p95 under 800ms
  },
};

export default function () {
  const zip = ZIPS[Math.floor(Math.random() * ZIPS.length)];
  const spec = SPECS[Math.floor(Math.random() * SPECS.length)];
  const res = http.get(`${BASE}/api/providers/search?zip=${zip}&taxonomy=${encodeURIComponent(spec)}&limit=25`);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'has providers array': (r) => { try { return Array.isArray(r.json().providers); } catch { return false; } },
  });
  sleep(Math.random() * 1.5 + 0.5);
}
