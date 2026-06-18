/* CareFind service worker (D1): installable PWA + offline app shell.
 *
 * - Precache the shell (page, bundle, pure logic, manifest, icon) so the app loads
 *   offline. Bump CACHE on any shell change; old caches are purged on activate.
 * - /api/* GET: network-first with a cache fallback, so the LAST search a user ran is
 *   still available offline (then refreshed when back online). Non-GET is passthrough.
 * - Map tiles + CDN libs: cache-first into a capped runtime cache (politely bounded).
 */
const CACHE = 'carefind-shell-v1';
const RUNTIME = 'carefind-runtime-v1';
const RUNTIME_MAX = 120; // cap runtime entries (tiles/libs) so storage can't grow unbounded
const SHELL = ['/', '/carefind.bundle.js', '/carefind.logic.js', '/manifest.webmanifest', '/carefind-icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== RUNTIME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function trim(cacheName, max) {
  const c = await caches.open(cacheName);
  const keys = await c.keys();
  for (let i = 0; i < keys.length - max; i++) await c.delete(keys[i]);
}

async function networkFirst(req) {
  try {
    const res = await fetch(req);
    if (res && res.ok) {
      const c = await caches.open(RUNTIME);
      c.put(req, res.clone());
    }
    return res;
  } catch (err) {
    const cached = await caches.match(req);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(req) {
  const cached = await caches.match(req);
  if (cached) return cached;
  const res = await fetch(req);
  if (res && (res.ok || res.type === 'opaque')) {
    const c = await caches.open(RUNTIME);
    c.put(req, res.clone());
    trim(RUNTIME, RUNTIME_MAX);
  }
  return res;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return; // never cache POST (geocode batch etc.)
  const url = new URL(req.url);

  // Same-origin API: network-first so a fresh search wins, but a prior one survives offline.
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) {
    e.respondWith(networkFirst(req));
    return;
  }
  // Same-origin shell + navigations: cache-first, fall back to the cached page offline.
  if (url.origin === self.location.origin) {
    if (req.mode === 'navigate') {
      e.respondWith(networkFirst(req).catch(() => caches.match('/')));
    } else {
      e.respondWith(cacheFirst(req));
    }
    return;
  }
  // Cross-origin map tiles / CDN libraries: cache-first, capped.
  if (/basemaps\.cartocdn\.com|tile\.openstreetmap\.org|unpkg\.com|jsdelivr\.net|cloudflare\.com/.test(url.host)) {
    e.respondWith(cacheFirst(req));
  }
});
