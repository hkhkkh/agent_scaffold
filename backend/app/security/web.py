"""HTTP-level protections for public mode: body cap, origin, host, headers."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.security.policy import language_for, message
from app.settings import get_settings

_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://challenges.cloudflare.com; "
    "frame-src https://challenges.cloudflare.com; "
    "connect-src 'self' https://challenges.cloudflare.com; "
    "img-src 'self' data: https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _json_body(code: str, detail: str) -> bytes:
    return json.dumps({"code": code, "detail": detail}, ensure_ascii=False).encode("utf-8")


async def _send_json(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """A pure ASGI body cap.

    BaseHTTPMiddleware would consume the request body, which is exactly wrong
    for SSE and for any handler that streams its input. This wrapper checks the
    declared length first and counts streamed bytes second; once the cap is hit
    it answers 413 and ignores whatever the inner application tries to send.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        settings = get_settings()
        if (
            scope.get("type") != "http"
            or not settings.public_access
            or settings.max_request_bytes <= 0
        ):
            await self.app(scope, receive, send)
            return

        max_bytes = settings.max_request_bytes
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    await _send_json(
                        send,
                        413,
                        _json_body("payload_too_large", "The request body is too large."),
                    )
                    return
            except ValueError:
                pass

        received = 0
        limit_exceeded = False

        async def wrapped_send(message: dict[str, Any]) -> None:
            if limit_exceeded:
                return
            await send(message)

        async def wrapped_receive() -> dict[str, Any]:
            nonlocal received, limit_exceeded
            if limit_exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > max_bytes:
                    limit_exceeded = True
                    await _send_json(
                        send,
                        413,
                        _json_body("payload_too_large", "The request body is too large."),
                    )
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, wrapped_receive, wrapped_send)


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Reject cross-site writes even though the gate cookie is SameSite=Lax.

    The `X-Scaffold-Client` header is not a credential — any non-browser client
    can set it. Its job is CSRF: a browser on another origin cannot attach it
    without a CORS preflight, which this deployment does not grant.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        if not settings.public_access or request.method.upper() in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        path = request.url.path
        if path.startswith("/api/access/") or path.startswith("/api/hooks/"):
            return await call_next(request)
        if getattr(request.state, "admin", False) or getattr(
            request.state, "gate_bypass", False
        ):
            return await call_next(request)

        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return self._deny(request)

        expected = settings.public_origin.rstrip("/")
        origin = (request.headers.get("origin") or "").strip()
        if origin:
            if origin.lower() == "null" or not _same_origin(origin, expected):
                return self._deny(request)
            return await call_next(request)

        referer = (request.headers.get("referer") or "").strip()
        if referer:
            if not _same_origin(referer, expected):
                return self._deny(request)
            return await call_next(request)

        if request.headers.get("x-scaffold-client", "").lower() == "web":
            return await call_next(request)
        return self._deny(request)

    def _deny(self, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "code": "origin_forbidden",
                "detail": message("origin_forbidden", language_for(request)),
            },
        )


class HostCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        if not settings.public_access:
            return await call_next(request)
        host = (request.url.hostname or "").lower()
        if host and host in settings.resolved_trusted_hosts:
            return await call_next(request)
        return JSONResponse(
            status_code=400,
            content={
                "code": "host_forbidden",
                "detail": message("host_forbidden", language_for(request)),
            },
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        response = await call_next(request)
        if not settings.public_access:
            return response

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        if settings.public_origin_is_https and settings.hsts_max_age > 0:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.hsts_max_age}; includeSubDomains",
            )

        path = request.url.path
        if not response.headers.get("Cache-Control"):
            if path.startswith("/assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            elif path in ("/", "/index.html"):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


def _same_origin(candidate: str, expected: str) -> bool:
    if not expected:
        return False
    try:
        left = urlsplit(candidate)
        right = urlsplit(expected)
    except ValueError:
        return False
    return (
        left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and (left.port or _default_port(left.scheme)) == (right.port or _default_port(right.scheme))
    )


def _default_port(scheme: str) -> int:
    return 443 if scheme.lower() == "https" else 80


__all__ = [
    "BodySizeLimitMiddleware",
    "HostCheckMiddleware",
    "OriginCheckMiddleware",
    "SecurityHeadersMiddleware",
]
