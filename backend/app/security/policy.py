"""Public-deployment policy: what a gate-only visitor may do.

The existing authentication layer answers "who is calling". This module answers
a different question: "this caller passed the human-verification gate, but is
this a thing they are allowed to do?" The two are deliberately separate because
public mode runs with ``AUTH_MODE=local`` — the operator is not the only caller,
so the local principal's implicit admin scope must not be trusted.

Everything here is a no-op unless ``PUBLIC_ACCESS=true``. A laptop run keeps the
old behaviour exactly.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from typing import Any

from fastapi import Depends, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.graph.schema import WorkflowGraph, normalize_language
from app.runtime.ai.messages import render
from app.settings import Settings, get_settings

logger = logging.getLogger("app.security.public")

#: Paths that may be reached without a gate cookie. They either disclose nothing
#: (status/health) or are the gate itself (challenge/verify).
GATE_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/health",
        "/api/access/status",
        "/api/access/challenge",
        "/api/access/verify",
    }
)

#: Prefixes that carry their own credential. Hooks are disabled in demo mode
#: anyway; keeping the prefix here means the gate does not double-protect them
#: in a non-demo public deployment.
GATE_PUBLIC_PREFIXES: tuple[str, ...] = ("/api/hooks/",)

_DOCS_PATHS: frozenset[str] = frozenset(
    {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)

_MESSAGES: dict[str, dict[str, str]] = {
    "gate_required": {
        "en": "Human verification is required before using this deployment.",
        "zh": "使用此部署前需要先完成人机验证。",
    },
    "admin_required": {
        "en": "This action is restricted to the deployment operator.",
        "zh": "此操作仅限部署者使用。",
    },
    "demo_disabled": {
        "en": "This feature is disabled in the public demo.",
        "zh": "该功能在公开演示中已禁用。",
    },
    "origin_forbidden": {
        "en": "The request origin is not allowed.",
        "zh": "请求来源不被允许。",
    },
    "rate_limited": {
        "en": "Too many requests. Please wait and try again.",
        "zh": "请求过于频繁，请稍后再试。",
    },
    "quota_exceeded": {
        "en": "Today's free demo allowance is used up. Please come back tomorrow.",
        "zh": "今天的免费演示额度已用完，请明天再来。",
    },
    "payload_too_large": {
        "en": "The request body is too large.",
        "zh": "请求体过大。",
    },
    "turnstile_failed": {
        "en": "Human verification failed. Try the fallback challenge.",
        "zh": "人机验证失败，请尝试备用验证。",
    },
    "pow_invalid": {
        "en": "The proof-of-work answer is invalid.",
        "zh": "工作量证明答案无效。",
    },
    "challenge_expired": {
        "en": "The challenge has expired. Request a new one.",
        "zh": "验证已过期，请重新获取。",
    },
    "challenge_used": {
        "en": "This challenge has already been used.",
        "zh": "此验证已被使用。",
    },
    "host_forbidden": {
        "en": "This host is not served by this deployment.",
        "zh": "此域名不属于当前部署。",
    },
    "not_found": {
        "en": "Not found.",
        "zh": "未找到。",
    },
}


def language_for(request: Request) -> str:
    asked = request.query_params.get("lang") or request.headers.get("accept-language") or ""
    return normalize_language(asked)


def message(code: str, lang: str = "en", **kwargs: Any) -> str:
    return render(_MESSAGES, code, lang, **kwargs)


class AccessDenied(Exception):
    """A policy refusal that the application turns into a JSON error.

    A dedicated exception rather than ``HTTPException`` because the body carries
    a stable ``code`` the frontend reacts to (``gate_required`` reopens the gate,
    ``demo_disabled`` explains a read-only workflow) and because the message
    needs the same en/zh treatment as everything else.
    """

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 403,
        detail: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.code = code
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}


def denied_response(code: str, request: Request, *, status_code: int = 403) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": message(code, language_for(request))},
    )


def client_ip(request: Request) -> str:
    """The best available client address behind Cloudflare Tunnel.

    Cloudflare sets ``CF-Connecting-IP``; a local process connecting directly to
    the loopback port can spoof it, but such a process is already inside the
    trust boundary. The header is only used for rate-limit keys, never for
    authorization.
    """
    candidates = [
        request.headers.get("cf-connecting-ip"),
        (request.headers.get("x-forwarded-for") or "").split(",")[0].strip(),
        request.client.host if request.client else "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate.strip()))
        except ValueError:
            continue
    return "unknown"


def limiter_key(request: Request) -> str:
    """A rate-limit key. IPv6 is grouped by /64; one host rarely owns a whole /64."""
    address = client_ip(request)
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def admin_token_valid(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    presented = request.headers.get("x-admin-token") or ""
    expected = settings.admin_token or ""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def gate_bypass_valid(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    presented = request.headers.get("x-scaffold-gate-bypass") or ""
    expected = settings.gate_bypass_token or ""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def is_admin_path(path: str, method: str) -> bool:
    """Paths that a public-demo visitor must not reach, even after the gate."""
    if path.startswith("/api/settings") or path.startswith("/api/schedules"):
        return True
    if path == "/api/metrics":
        return True
    if path.startswith("/api/workflows/tools/check"):
        return True
    if path == "/api/workflows/import":
        return True
    if path.startswith("/api/workflows/") and (
        path.endswith("/export") or path.endswith("/export-agent")
    ):
        return True
    if method.upper() == "DELETE" and path.startswith("/api/workflows/"):
        return True
    if path.startswith("/api/ai/node-to-code") or path.startswith("/api/ai/code-to-node"):
        return True
    return False


def graphs_with_code(graph: WorkflowGraph) -> list[str]:
    return [node.id for node in graph.nodes if node.type == "code"]


def enforce_graph_limits(graph: WorkflowGraph, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.max_nodes_per_workflow and len(graph.nodes) > settings.max_nodes_per_workflow:
        raise AccessDenied(
            "demo_disabled",
            status_code=413,
            detail=(
                f"This deployment allows at most {settings.max_nodes_per_workflow} "
                f"nodes; the graph has {len(graph.nodes)}."
            ),
        )
    if settings.max_edges_per_workflow and len(graph.edges) > settings.max_edges_per_workflow:
        raise AccessDenied(
            "demo_disabled",
            status_code=413,
            detail=(
                f"This deployment allows at most {settings.max_edges_per_workflow} "
                f"edges; the graph has {len(graph.edges)}."
            ),
        )


def enforce_public_graph_policy(
    graph: WorkflowGraph,
    settings: Settings | None = None,
    *,
    admin: bool = False,
) -> None:
    """Refuse writes/runs that the safe demo does not allow.

    Validation deliberately does *not* call this: a workflow containing Code
    nodes must remain openable and readable. The refusal belongs at the write and
    run boundaries, where the operation would actually execute or persist it.
    """
    settings = settings or get_settings()
    if not settings.public_demo or admin:
        return
    enforce_graph_limits(graph, settings)
    if graph.tools:
        raise AccessDenied(
            "demo_disabled",
            detail="Custom tool modules are disabled in the public demo.",
        )
    code_nodes = graphs_with_code(graph)
    if code_nodes:
        raise AccessDenied(
            "demo_disabled",
            detail=(
                "This workflow contains Code nodes, which are display-only in the "
                "public demo. It can be viewed but not saved or run."
            ),
        )
    from app.nodes.integrations import PUBLIC_DEMO_DISABLED_TYPES

    private_connectors = sorted(
        {node.type for node in graph.nodes if node.type in PUBLIC_DEMO_DISABLED_TYPES}
    )
    if private_connectors:
        raise AccessDenied(
            "demo_disabled",
            detail=(
                "This workflow uses private connectors that are disabled in the "
                f"public demo: {', '.join(private_connectors)}. It can be viewed "
                "but not saved or run."
            ),
        )


def public_capabilities(settings: Settings | None = None) -> dict[str, bool]:
    settings = settings or get_settings()
    demo = settings.public_demo
    return {
        "public_demo": demo,
        "code_nodes": bool(settings.code_node_enabled) and not demo,
        "custom_tools": not demo,
        "settings": not demo,
        "schedules": not demo,
        "exports": not demo,
        "workflow_delete": not demo,
        "metrics": not demo,
        "admin": bool(settings.admin_token),
    }


def require_admin(request: Request) -> None:
    """FastAPI dependency for owner-only routes.

    A no-op outside public mode, where the existing auth layer is the boundary.
    In public mode the operator's header is checked here because the local
    principal has every scope and therefore cannot express "operator only".
    """
    settings = get_settings()
    if not settings.public_access:
        return
    if admin_token_valid(request, settings):
        return
    raise AccessDenied("admin_required")


class PublicDemoMiddleware(BaseHTTPMiddleware):
    """Set request state and block admin paths before the gate is consulted."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        if not settings.public_access:
            return await call_next(request)

        request.state.admin = admin_token_valid(request, settings)
        request.state.gate_bypass = gate_bypass_valid(request, settings)
        path = request.url.path

        if path in _DOCS_PATHS:
            return denied_response("not_found", request, status_code=404)

        if settings.public_demo and path.startswith("/api/hooks/"):
            # A valid hook token would otherwise start a run without the gate.
            # Returning 404 keeps "exists but disabled" indistinguishable from
            # "no such hook", matching the hook endpoint's own policy.
            return denied_response("not_found", request, status_code=404)

        if (
            not request.state.admin
            and not request.state.gate_bypass
            and is_admin_path(path, request.method)
        ):
            return denied_response("admin_required", request)

        return await call_next(request)


def graph_is_read_only(graph: WorkflowGraph, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.public_demo:
        return False
    return bool(graph.tools) or bool(graphs_with_code(graph))


__all__ = [
    "AccessDenied",
    "GATE_PUBLIC_PATHS",
    "GATE_PUBLIC_PREFIXES",
    "admin_token_valid",
    "client_ip",
    "denied_response",
    "enforce_graph_limits",
    "enforce_public_graph_policy",
    "gate_bypass_valid",
    "graph_is_read_only",
    "graphs_with_code",
    "is_admin_path",
    "language_for",
    "limiter_key",
    "message",
    "public_capabilities",
    "require_admin",
]
