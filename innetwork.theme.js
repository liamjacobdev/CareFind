/* Pre-paint theme init. Sets <html data-theme> from the saved choice (or the OS
   preference when the user hasn't chosen) BEFORE the body paints, so dark-mode users
   never see a flash of the light theme. Deliberately tiny and dependency-free, and
   kept as an external same-origin script in <head> so the strict CSP (no
   'unsafe-inline' for script-src) still holds. The full toggle lives in the bundle. */
(function () {
  try {
    var saved = localStorage.getItem('innetwork_theme');
    var dark = saved
      ? saved === 'dark'
      : !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (dark) document.documentElement.setAttribute('data-theme', 'dark');
  } catch (_e) {
    /* private mode / blocked storage — default light, no flash risk */
  }
})();
