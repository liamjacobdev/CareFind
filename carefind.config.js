/* Deployment config, loaded as a same-origin <script src> before the bundle so the page
 * carries NO inline executable script — letting the CSP drop 'unsafe-inline' from
 * script-src (D3). configure_frontend.py rewrites these values per environment. */
window.CAREFIND_CONFIG = {
  apiBase: 'http://localhost:8000',
  claimEmail: 'providers@carefind.example',
  allowPublicProxies: false,
};
