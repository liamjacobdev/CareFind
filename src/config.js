/* Deployment configuration — the only values that change per environment.
 *
 * Read from `window.CAREFIND_CONFIG`, a tiny object the page injects before the
 * bundle runs (see the inline config block in carefind.html). configure_frontend.py
 * rewrites those values for production so the API origin, claim inbox, and CSP never
 * drift. Defaults below mirror the local-dev setup so the served page works out of
 * the box at http://localhost:8000.
 */
const C = (typeof window !== 'undefined' && window.CAREFIND_CONFIG) || {};

// Where the page sends API requests. Empty -> same-origin only (standalone).
export const API_BASE = (C.apiBase != null ? C.apiBase : 'http://localhost:8000');
export const HAS_BACKEND = !!API_BASE;

// Opt-in public CORS proxies (kept off + out of the CSP by default; see README).
export const ALLOW_PUBLIC_PROXIES = !!C.allowPublicProxies;

// The "claim your listing" inbox. While it's the placeholder, claim affordances stay
// hidden so the page never renders a dead mailto:.
const PLACEHOLDER_CLAIM_EMAIL = 'providers@carefind.example';
export const CLAIM_EMAIL = (C.claimEmail != null ? C.claimEmail : PLACEHOLDER_CLAIM_EMAIL);
export const CLAIM_ENABLED = CLAIM_EMAIL !== PLACEHOLDER_CLAIM_EMAIL;

// Official registries the page can reach directly (both send CORS headers).
export const NPI_API = 'https://npiregistry.cms.hhs.gov/api/';
export const NOMINATIM = 'https://nominatim.openstreetmap.org';

// Whether the page is being served over http(s) (vs. opened from file://).
export const SERVED = (typeof location !== 'undefined') &&
  (location.protocol === 'http:' || location.protocol === 'https:');
export const SAME_ORIGIN = SERVED ? location.origin : '';
