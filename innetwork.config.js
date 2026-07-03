/* Deployment config, loaded as a same-origin <script src> before the bundle so the page
 * carries NO inline executable script — letting the CSP drop 'unsafe-inline' from
 * script-src (D3). configure_frontend.py rewrites these values per environment. */
window.INNETWORK_CONFIG = {
  apiBase: 'https://innetwork.vercel.app',
  claimEmail: 'providers@innetwork.example',
  allowPublicProxies: false,
};
