// Runtime globals checkJs needs to know about: Leaflet (loaded from a CDN, on window),
// the injected deployment config, and a couple of ad-hoc props the app attaches.
// Loose by design — this is bug-catching, not full typing.
declare const L: any;
interface Window {
  L?: any;
  CAREFIND_CONFIG?: { apiBase?: string; claimEmail?: string; allowPublicProxies?: boolean };
}
