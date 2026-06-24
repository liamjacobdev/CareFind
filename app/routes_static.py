"""Static-asset routes (split from main.py): the page, the built bundle + injected
config, the shared pure logic, and the app icon. Each is served with an ETag + short
Cache-Control (304 on a matching If-None-Match)."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

router = APIRouter()


_FRONTEND = Path(__file__).resolve().parent.parent / "carefind.html"


_FRONTEND_LOGIC = _FRONTEND.parent / "carefind.logic.js"
_FRONTEND_BUNDLE = _FRONTEND.parent / "carefind.bundle.js"
_FRONTEND_CONFIG = _FRONTEND.parent / "carefind.config.js"
_FRONTEND_THEME = _FRONTEND.parent / "carefind.theme.js"
_ICON = _FRONTEND.parent / "carefind-icon.svg"


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
                        "Frontend (carefind.html) not found next to the app package.")


@router.get("/carefind.config.js")
def frontend_config(request: Request) -> Response:
    # Deployment config (data only), external so the page has no inline script (D3).
    return _static_file(request, _FRONTEND_CONFIG, "application/javascript",
                        "carefind.config.js not found next to the app package.")


@router.get("/carefind.theme.js")
def frontend_theme(request: Request) -> Response:
    # Pre-paint theme init (data-theme from saved choice / OS), in <head> so dark-mode
    # users see no flash of light. External + same-origin to keep the strict CSP.
    return _static_file(request, _FRONTEND_THEME, "application/javascript",
                        "carefind.theme.js not found next to the app package.")


@router.get("/carefind.bundle.js")
def frontend_bundle(request: Request) -> Response:
    # The page's interactive layer, bundled from src/ by `npm run build` (esbuild).
    return _static_file(request, _FRONTEND_BUNDLE, "application/javascript",
                        "carefind.bundle.js not found — run `npm run build`.")


@router.get("/carefind.logic.js")
def frontend_logic(request: Request) -> Response:
    # The shared pure logic module — a build input (bundled into carefind.bundle.js)
    # and the unit-tested source (Vitest). Still served for source transparency.
    return _static_file(request, _FRONTEND_LOGIC, "application/javascript",
                        "carefind.logic.js not found next to the app package.")


@router.get("/carefind-icon.svg")
def app_icon(request: Request) -> Response:
    return _static_file(request, _ICON, "image/svg+xml",
                        "carefind-icon.svg not found next to the app package.")

