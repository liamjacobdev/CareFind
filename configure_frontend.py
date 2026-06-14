"""Point carefind.html at your deployed API in one step.

Editing the frontend for production means changing two places that must agree:
  1. const API_BASE   — where the page sends requests
  2. the CSP connect-src — which origins the browser is allowed to reach

This script sets both from a single argument, so they never drift.

Usage:
    python configure_frontend.py https://api.yourdomain.com
    python configure_frontend.py https://api.yourdomain.com --out carefind.prod.html
"""
import argparse
import re
import sys
from pathlib import Path

SRC = Path(__file__).parent / "carefind.html"


# The public CORS-proxy fallbacks the standalone (no-backend) page can opt into.
# Kept OUT of the default CSP (ALLOW_PUBLIC_PROXIES is false) to shrink attack
# surface; re-added here only when --allow-public-proxies is passed.
PUBLIC_PROXY_ORIGINS = [
    "https://api.codetabs.com",
    "https://api.allorigins.win",
    "https://corsproxy.io",
]


def configure(api_base: str, out: Path, allow_public_proxies: bool = False) -> None:
    api_base = api_base.rstrip("/")
    html = SRC.read_text(encoding="utf-8")

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
    print(f"Wrote {out} — API_BASE and CSP connect-src now point at {api_base}{proxies}")


def main(argv) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("api_base", help="e.g. https://api.yourdomain.com")
    ap.add_argument("--out", default="carefind.html",
                    help="output file (default: overwrite carefind.html)")
    ap.add_argument("--allow-public-proxies", action="store_true",
                    help="opt into the public CORS-proxy fallbacks (adds them to the "
                         "CSP and flips ALLOW_PUBLIC_PROXIES). Off by default.")
    args = ap.parse_args(argv[1:])
    configure(args.api_base, Path(args.out), allow_public_proxies=args.allow_public_proxies)


if __name__ == "__main__":
    main(sys.argv)
