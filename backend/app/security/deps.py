"""Authentication wiring: state, middleware, and the FastAPI dependency.

Why middleware rather than a per-route dependency
------------------------------------------------

A dependency has to be remembered. Every new router, every new endpoint added in
a hurry, is an opportunity to forget ``dependencies=[Depends(require_auth)]`` and
silently publish an endpoint. Middleware inverts that: the default is *closed*,
`/api/*` is protected unless it is named in :data:`~app.security.PUBLIC_PATHS`,
and forgetting to do something is safe.

That is the same reasoning as the path-traversal fix — make the safe thing the
thing that happens by default — applied to the request pipeline.

The check order is deliberate too. A token is verified *before* the scope is
consulted, so an invalid token can never distinguish "wrong credential" from
"insufficient permission" by the response it gets.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.runtime.ai.messages import render
from app.runtime.trace import (
    format_traceparent,
    new_trace_id,
    parse_traceparent,
    reset_trace_id,
    set_trace_id,
)
from app.security.auth import (
    LOCAL_PRINCIPAL,
    PUBLIC_PATHS,
    PUBLIC_PREFIXES,
    AuthError,
    Principal,
    TokenRecord,
    describe_posture,
    is_loopback,
    parse_tokens,
    resolve_auth_mode,
    salt_from_env,
)

logger = logging.getLogger("app.security")

#: Every sentence this module returns to a caller. Table-plus-lookup rather than
#: a `_zh` field on each string, so a message cannot ship in one language only:
#: `scripts/check_i18n.py` walks the table and fails on a missing pair.
#:
#: These are kept English-first because `render` falls back to `en`, and the
#: fallback matters here — a 401 is returned before anything about the request's
#: language can be trusted.
_MESSAGES: dict[str, dict[str, str]] = {
    "unauthorized": {
        "en": "A valid bearer token is required.",
        "zh": "需要有效的 bearer token。",
    },
    "local_only": {
        "en": (
            "This server is in local mode and only accepts requests from the "
            "machine it runs on. Set AUTH_MODE=token and configure AUTH_TOKENS "
            "to expose it."
        ),
        "zh": (
            "本服务器运行在本地模式，只接受本机请求。"
            "如需对外提供服务，请设置 AUTH_MODE=token 并配置 AUTH_TOKENS。"
        ),
    },
    "forbidden_scope": {
        "en": "This token needs the '{scope}' scope.",
        "zh": "此 token 需要 '{scope}' 权限。",
    },
    "auth_hint": {
        "en": (
            "Send 'Authorization: Bearer <token>'. Tokens are configured "
            "with AUTH_TOKENS as 'name:token[:scope|scope]'."
        ),
        "zh": (
            "请发送 'Authorization: Bearer <token>'。"
            "Token 通过 AUTH_TOKENS 配置，格式为 'name:token[:scope|scope]'。"
        ),
    },
}

#: Methods that must reach the application so CORS preflight can be answered.
#: A browser cannot attach an Authorization header to a preflight, so requiring
#: one there would make cross-origin token auth impossible.
_PREFLIGHT = frozenset({"OPTIONS"})


@dataclass
class AuthState:
    """Resolved authentication configuration for this process."""

    mode: str = "local"
    host: str = "127.0.0.1"
    salt: str = ""
    records: list[TokenRecord] = field(default_factory=list)
    #: Requests served, for the audit trail. Counts only; no payloads.
    rejections: int = 0

    def authenticate(self, token: str) -> Principal | None:
        """Match a presented token against every configured principal.

        Every record is compared, with no early exit, so the time taken does not
        reveal *which* principal was matched — or how close a guess was.
        """
        if not token:
            return None
        match: TokenRecord | None = None
        for record in self.records:
            from app.security.auth import hash_token

            candidate = hash_token(token, self.salt)
            if hmac.compare_digest(candidate, record.token_hash):
                match = match or record
        if match is None:
            return None
        return Principal(name=match.name, scopes=match.scopes)

    def posture(self) -> str:
        return describe_posture(self.mode, self.host, len(self.records))


#: Process-wide state, populated by :func:`configure`.
_state = AuthState()


def configure(
    *,
    mode: str,
    host: str,
    token_specs: list[str] | None = None,
    salt: str | None = None,
) -> AuthState:
    """Resolve and install the authentication configuration.

    Called once from the application lifespan. Raises :class:`AuthError` for a
    configuration that cannot be honoured, so a misconfiguration stops the server
    rather than starting it in a weaker state than intended.
    """
    global _state

    effective = resolve_auth_mode(mode, host)
    resolved_salt = salt if salt is not None else salt_from_env()
    records = parse_tokens(token_specs or [], resolved_salt)

    if effective == "token" and not records:
        raise AuthError(
            "AUTH_MODE=token requires at least one token. Set AUTH_TOKENS to "
            "'name:token' entries, or bind to 127.0.0.1 to run without "
            "authentication. Refusing to start an open control plane."
        )

    _state = AuthState(
        mode=effective,
        host=host,
        salt=resolved_salt,
        records=records,
    )
    return _state


def get_state() -> AuthState:
    return _state


def get_principal(request: Request) -> Principal:
    """FastAPI dependency exposing the authenticated caller."""
    principal = getattr(request.state, "principal", None)
    return principal or LOCAL_PRINCIPAL


def _extract_bearer(request: Request) -> str:
    """Read a token from ``Authorization: Bearer`` or ``X-API-Key``.

    Two accepted forms because they serve different clients: the ``Authorization``
    header is what a browser and ``curl -H`` use, while ``X-API-Key`` is what a
    workflow's HTTP tool can set without thinking about header syntax.
    """
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _language(request: Request) -> str:
    """Which language to answer in.

    Read from the query string or ``Accept-Language`` rather than from FastAPI's
    dependency system, because a middleware cannot have dependencies. The query
    string wins: it is what the canvas sets explicitly when the user picks a
    language, while ``Accept-Language`` is whatever the browser happened to send.
    """
    asked = request.query_params.get("lang") or request.headers.get("accept-language") or ""
    from app.graph.schema import normalize_language

    return normalize_language(asked)


def _unauthorized(reason_key: str, lang: str = "en") -> JSONResponse:
    """A 401 whose wording follows the request's language.

    Translated by key rather than passed in as prose so the same message cannot
    drift between the two languages, and so `check_i18n.py` can see every pair.
    """
    return JSONResponse(
        status_code=401,
        content={
            "detail": render(_MESSAGES, reason_key, lang),
            "hint": render(_MESSAGES, "auth_hint", lang),
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(required: str, principal: Principal, lang: str = "en") -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "detail": render(_MESSAGES, "forbidden_scope", lang, scope=required),
            "principal": principal.name,
            "scopes": sorted(principal.scopes),
        },
    )


#: Where the trace id comes back to a caller. The same header the W3C spec uses,
#: so a client that already understands `traceparent` needs nothing new.
TRACE_HEADER = "traceparent"


class TraceMiddleware(BaseHTTPMiddleware):
    """Give every request an id, and return it to the caller.

    Placed **outside** auth so a rejected request still gets one — a 401 with no
    id is exactly the request an operator wants to trace. Starlette runs
    middleware in the order added with the last added outermost, so `main.py`
    adds this after `AuthMiddleware`.

    An incoming `traceparent` is honoured when it is well-formed, which is what
    makes a trace span more than one service. When it is missing or malformed the
    spec says to start a new trace, and that is what happens: a caller can send
    anything without affecting the id this process uses.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        incoming = parse_traceparent(request.headers.get(TRACE_HEADER))
        trace_id = incoming or new_trace_id()
        token = set_trace_id(trace_id)
        try:
            response = await call_next(request)
            # Echoed so a client can put it in a bug report. The parent-id is
            # this server's span, fresh on every response.
            response.headers[TRACE_HEADER] = format_traceparent(trace_id)
            return response
        finally:
            reset_trace_id(token)


class AuthMiddleware(BaseHTTPMiddleware):
    """Deny-by-default authentication for everything under ``/api``.

    Paths outside ``/api`` — the SPA itself, static assets — are untouched: the
    frontend is not a secret, and gating it would break the browser's initial
    load before any token could be supplied.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        state = get_state()
        path = request.url.path

        # `startswith` accepts a tuple, so both exemptions are one check. The prefix
        # list is empty by default and holds one entry — see PUBLIC_PREFIXES for
        # what guards the paths it covers.
        if (
            not path.startswith("/api")
            or path in PUBLIC_PATHS
            or path.startswith(PUBLIC_PREFIXES)
        ):
            request.state.principal = LOCAL_PRINCIPAL if state.mode == "local" else None
            return await call_next(request)

        if request.method in _PREFLIGHT:
            return await call_next(request)

        if state.mode == "local":
            # Not an absence of a check. Two independent conditions must hold:
            # `resolve_auth_mode` has already proven the *configured* bind is
            # loopback, and the peer address is verified here.
            #
            # The second check is defence in depth against the one mistake the
            # first cannot see: `SERVER_HOST` saying 127.0.0.1 while uvicorn was
            # actually told `--host 0.0.0.0`. Without it, a single mismatched
            # setting would publish an unauthenticated control plane.
            peer = request.client.host if request.client else ""
            if not is_loopback(peer):
                state.rejections += 1
                logger.warning(
                    "Refused %s from %s: AUTH_MODE=local but the peer is not "
                    "loopback. Set AUTH_MODE=token if this server should be "
                    "reachable from other machines.",
                    path,
                    peer,
                )
                return _unauthorized("local_only", _language(request))
            request.state.principal = LOCAL_PRINCIPAL
            return await call_next(request)

        principal = state.authenticate(_extract_bearer(request))
        if principal is None:
            state.rejections += 1
            return _unauthorized("unauthorized", _language(request))

        required = principal.required_scope(request.method, path)
        if not principal.has(required):
            state.rejections += 1
            return _forbidden(required, principal, _language(request))

        request.state.principal = principal
        return await call_next(request)


__all__ = [
    "AuthMiddleware",
    "AuthState",
    "configure",
    "get_principal",
    "get_state",
]
