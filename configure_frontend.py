"""Point carefind.html at your deployed API in one step.

Editing the frontend for production means changing two places that must agree:
  1. const API_BASE   — where the page sends requests
  2. the CSP connect-src — which origins the browser is allowed to reach

This script sets both from a single argument, so they never drift.

A third value, CLAIM_EMAIL (the "claim your listing" inbox), must also be set for
the provider-claim affordances to appear at all — the page hides them while it
still holds the placeholder. Set it here from --claim-email or $CAREFIND_CLAIM_EMAIL
so it can't drift either.

Usage:
    python configure_frontend.py https://api.yourdomain.com
    python configure_frontend.py https://api.yourdomain.com --claim-email you@real.com
    python configure_frontend.py https://api.yourdomain.com --out carefind.prod.html
"""
import argparse
import os
import re
import sys
from pathlib import Path

SRC = Path(__file__).parent / "carefind.html"

# Mirrors the placeholder in carefind.html; the page hides claim affordances while
# CLAIM_EMAIL equals this, so swapping it in is what turns the feature on.
PLACEHOLDER_CLAIM_EMAIL = "providers@carefind.example"


# The public CORS-proxy fallbacks the standalone (no-backend) page can opt into.
# Kept OUT of the default CSP (ALLOW_PUBLIC_PROXIES is false) to shrink attack
# surface; re-added here only when --allow-public-proxies is passed.
PUBLIC_PROXY_ORIGINS = [
    "https://api.codetabs.com",
    "https://api.allorigins.win",
    "https://corsproxy.io",
]


def configure(api_base: str, out: Path, allow_public_proxies: bool = False,
              claim_email: str = None) -> None:
    api_base = api_base.rstrip("/")
    html = SRC.read_text(encoding="utf-8")

    # 0) CLAIM_EMAIL — rewrite the constant so it never drifts from API_BASE/CSP.
    # Only when a real address is supplied; otherwise leave the placeholder in
    # place (the page hides claim affordances while it's the placeholder).
    if claim_email:
        new = re.sub(r"(?m)^const CLAIM_EMAIL\s*=\s*'[^']*';",
                     f"const CLAIM_EMAIL   = '{claim_email}';", html, count=1)
        if new == html:
            raise SystemExit("Could not find the CLAIM_EMAIL declaration to update.")
        html = new

    # 1) API_BASE — anchor to the real declaration at the start of a line, not the
    # "// e.g. const API_BASE = ..." comment above it.
    new = re.sub(r"(?m)^const API_BASE\s*=\s*'[^']*';",
                 f"const API_BASE      = '{api_base}';", html, count=1)
    if new == html:
        raise SystemExit("Could not find the API_BASE declaration to update.")
    html = new

    # 2) CSP connect-src: drop the localhost dev origins, add the real API origin.
    def fix_connect(m):
        directive = m.group(0)
        directive = directive.replace("http://localhost:8000 ", "")
        directive = directive.replace("http://localhost:8787 ", "")
        if api_base not in directive:
            directive = directive.replace("connect-src 'self' ",
                                          f"connect-src 'self' {api_base} ")
        # Re-add the public CORS-proxy origins ONLY when explicitly opted in, so the
        # CSP and the ALLOW_PUBLIC_PROXIES toggle below stay in agreement.
        if allow_public_proxies:
            extra = " ".join(o for o in PUBLIC_PROXY_ORIGINS if o not in directive)
            if extra:
                directive = directive.replace(";", f" {extra};", 1)
        return directive

    html = re.sub(r"connect-src[^;]*;", fix_connect, html, count=1)

    # Keep the JS opt-in flag in lockstep with the CSP so enabling proxies actually
    # works (the page won't reach an origin the CSP forbids, and vice-versa).
    if allow_public_proxies:
        html = re.sub(r"(?m)^const ALLOW_PUBLIC_PROXIES\s*=\s*false;",
                      "const ALLOW_PUBLIC_PROXIES = true;", html, count=1)

    out.write_text(html, encoding="utf-8")
    proxies = " (public CORS proxies enabled)" if allow_public_proxies else ""
    claim = f"; CLAIM_EMAIL set to {claim_email}" if claim_email else ""
    print(f"Wrote {out} — API_BASE and CSP connect-src now point at {api_base}{proxies}{claim}")


def main(argv) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("api_base", help="e.g. https://api.yourdomain.com")
    ap.add_argument("--out", default="carefind.html",
                    help="output file (default: overwrite carefind.html)")
    ap.add_argument("--allow-public-proxies", action="store_true",
                    help="opt into the public CORS-proxy fallbacks (adds them to the "
                         "CSP and flips ALLOW_PUBLIC_PROXIES). Off by default.")
    ap.add_argument("--claim-email", default=os.environ.get("CAREFIND_CLAIM_EMAIL"),
                    help="real inbox for provider 'claim your listing' requests "
                         "(or set $CAREFIND_CLAIM_EMAIL). Until set, the page hides "
                         "all claim affordances rather than showing a dead mailto.")
    args = ap.parse_args(argv[1:])
    claim_email = (args.claim_email or "").strip() or None
    if claim_email == PLACEHOLDER_CLAIM_EMAIL:
        raise SystemExit(f"--claim-email is still the placeholder ({PLACEHOLDER_CLAIM_EMAIL}); "
                         "supply a real inbox or omit it to keep claim affordances hidden.")
    configure(args.api_base, Path(args.out),
              allow_public_proxies=args.allow_public_proxies, claim_email=claim_email)


if __name__ == "__main__":
    main(sys.argv)
