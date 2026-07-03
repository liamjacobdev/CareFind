"""Static-asset routes (split from main.py): the page, the built bundle + injected
config, the shared pure logic, and the app icon. Each is served with an ETag + short
Cache-Control (304 on a matching If-None-Match)."""
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .config import settings

router = APIRouter()


_FRONTEND = Path(__file__).resolve().parent.parent / "innetwork.html"


_FRONTEND_LOGIC = _FRONTEND.parent / "innetwork.logic.js"
_FRONTEND_BUNDLE = _FRONTEND.parent / "innetwork.bundle.js"
_FRONTEND_CONFIG = _FRONTEND.parent / "innetwork.config.js"
_FRONTEND_THEME = _FRONTEND.parent / "innetwork.theme.js"
_ICON = _FRONTEND.parent / "innetwork-icon.svg"
_OG_IMAGE = _FRONTEND.parent / "innetwork-og.svg"


def _static_file(request: Request, path: Path, media_type: str, missing: str) -> Response:
    """Serve a static file with an ETag + short Cache-Control, and answer a matching
    If-None-Match with 304 so a repeat load isn't re-downloaded. The ETag is derived
    from the file's mtime+size, so editing the file invalidates caches automatically."""
    if not path.exists():
        raise HTTPException(404, missing)
    st = path.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers = {"Cache-Control": "public, max-age=300, must-revalidate", "ETag": etag}
    inm = request.headers.get("if-none-match", "")
    if etag in [t.strip() for t in inm.split(",") if t.strip()]:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/")
def index(request: Request) -> Response:
    return _static_file(request, _FRONTEND, "text/html",
                        "Frontend (innetwork.html) not found next to the app package.")


def _request_origin(request: Request) -> str:
    """The page's own origin, honoring the reverse proxy (Vercel/Caddy) that terminates
    TLS: trust X-Forwarded-Proto/Host, else fall back to the request URL."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() \
        or request.url.scheme or "https"
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() \
        or request.headers.get("host", "").strip() or request.url.netloc
    return f"{proto}://{host}" if host else ""


@router.get("/innetwork.config.js")
def frontend_config(request: Request) -> Response:
    # Deployment config (data only), external so the page has no inline script (D3).
    # When the same process serves page + API (INNETWORK_SAME_ORIGIN, e.g. on Vercel),
    # rewrite apiBase to the request's own origin so a fresh deploy works on first load
    # with no configure_frontend step. CSP 'self' already covers same-origin API calls.
    if settings.same_origin_frontend and _FRONTEND_CONFIG.exists():
        origin = _request_origin(request)
        if origin:
            body = _FRONTEND_CONFIG.read_text(encoding="utf-8")
            body = re.sub(r"apiBase:\s*'[^']*'", f"apiBase: '{origin}'", body, count=1)
            return Response(body, media_type="application/javascript",
                            headers={"Cache-Control": "no-store"})
    return _static_file(request, _FRONTEND_CONFIG, "application/javascript",
                        "innetwork.config.js not found next to the app package.")


@router.get("/innetwork.theme.js")
def frontend_theme(request: Request) -> Response:
    # Pre-paint theme init (data-theme from saved choice / OS), in <head> so dark-mode
    # users see no flash of light. External + same-origin to keep the strict CSP.
    return _static_file(request, _FRONTEND_THEME, "application/javascript",
                        "innetwork.theme.js not found next to the app package.")


@router.get("/innetwork.bundle.js")
def frontend_bundle(request: Request) -> Response:
    # The page's interactive layer, bundled from src/ by `npm run build` (esbuild).
    return _static_file(request, _FRONTEND_BUNDLE, "application/javascript",
                        "innetwork.bundle.js not found — run `npm run build`.")


@router.get("/innetwork.logic.js")
def frontend_logic(request: Request) -> Response:
    # The shared pure logic module — a build input (bundled into innetwork.bundle.js)
    # and the unit-tested source (Vitest). Still served for source transparency.
    return _static_file(request, _FRONTEND_LOGIC, "application/javascript",
                        "innetwork.logic.js not found next to the app package.")


@router.get("/innetwork-icon.svg")
def app_icon(request: Request) -> Response:
    return _static_file(request, _ICON, "image/svg+xml",
                        "innetwork-icon.svg not found next to the app package.")


@router.get("/innetwork-og.svg")
def og_image(request: Request) -> Response:
    # Social-share card (og:image / twitter:image). On Vercel the physical file is
    # static-served; this route serves it app-direct (local dev / self-host).
    return _static_file(request, _OG_IMAGE, "image/svg+xml",
                        "innetwork-og.svg not found next to the app package.")


@router.get("/favicon.ico")
def favicon(request: Request) -> Response:
    # Browsers and crawlers request /favicon.ico directly regardless of the <link> icon;
    # answer with the SVG mark (modern engines accept image/svg+xml here) instead of a 404.
    return _static_file(request, _ICON, "image/svg+xml",
                        "innetwork-icon.svg not found next to the app package.")


@router.get("/robots.txt")
def robots(request: Request) -> Response:
    # Allow crawling the app, keep bots out of the JSON API (no index value, wastes crawl
    # budget), and point them at the sitemap. Origin-aware so it's correct on any deploy.
    origin = _request_origin(request) or ""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        f"{'Sitemap: ' + origin + '/sitemap.xml' if origin else ''}\n"
    )
    return Response(body, media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/sitemap.xml")
def sitemap(request: Request) -> Response:
    # Minimal but valid sitemap for the single-page app. Structured as a real urlset so it
    # extends cleanly when specialty/city landing pages are added later.
    origin = _request_origin(request) or ""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{origin}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>\n"
        "</urlset>\n"
    )
    return Response(body, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=86400"})

