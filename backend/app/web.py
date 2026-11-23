"""Serve the built SPA from the same origin as the API.

The frontend is built by Vite; production does not need a second web server.
The catch-all is registered after every API router so an unknown ``/api/*`` is
still a JSON 404 rather than an HTML page.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, JSONResponse

from app.settings import Settings, get_settings

_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _safe_file(root: Path, relative: str) -> Path | None:
    try:
        resolved_root = root.resolve()
        candidate = (resolved_root / relative).resolve()
    except (OSError, RuntimeError):
        return None
    if candidate != resolved_root and resolved_root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def index_response(settings: Settings) -> FileResponse | JSONResponse:
    index = settings.frontend_dist / "index.html"
    if not index.is_file():
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "detail": "The frontend build is missing."},
        )
    return FileResponse(
        index,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def landing_response(settings: Settings) -> FileResponse | JSONResponse:
    """Serve the static marketing page at `/`; the editor lives at `/app`.

    A first-time visitor landing directly in an empty canvas is a conversion
    problem, not a feature. The landing page is plain HTML (no script) so it is
    cheap to serve and index, and it degrades to the SPA shell if a build did
    not copy it.
    """
    landing = settings.frontend_dist / "landing.html"
    if not landing.is_file():
        return index_response(settings)
    return FileResponse(
        landing,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def register_spa_routes(app: FastAPI, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.public_access:
        return

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    def asset(asset_path: str):
        file = _safe_file(settings.frontend_dist / "assets", asset_path)
        if file is None:
            return JSONResponse(
                status_code=404,
                content={"code": "not_found", "detail": "Not found."},
            )
        return FileResponse(
            file,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api"):
            return JSONResponse(
                status_code=404,
                content={"code": "not_found", "detail": "Unknown API route."},
            )
        if f"/{full_path}" in _DOCS_PATHS:
            return JSONResponse(
                status_code=404,
                content={"code": "not_found", "detail": "Not found."},
            )

        # Files that live at the root of the build (favicon, manifest) are served
        # directly. Everything else is the SPA shell; there is no client router
        # today, but the fallback is what makes adding one safe.
        if full_path:
            file = _safe_file(settings.frontend_dist, full_path)
            if file is not None:
                cache = (
                    "public, max-age=31536000, immutable"
                    if full_path.startswith("assets/")
                    else "no-cache"
                )
                return FileResponse(file, headers={"Cache-Control": cache})
        return index_response(settings)


__all__ = ["index_response", "landing_response", "register_spa_routes"]
