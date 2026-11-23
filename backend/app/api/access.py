"""Human-verification endpoints and the gate's public state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from app.security.gate import (
    consume_nonce,
    cookie_payload,
    issue_challenge,
    issue_cookie,
    set_gate_cookie,
    verify_challenge,
    verify_pow,
    verify_turnstile,
)
from app.security.policy import (
    AccessDenied,
    client_ip,
    public_capabilities,
)
from app.security.quotas import (
    issue_visitor,
    quota_status,
    set_visitor_cookie,
    visitor_id,
)
from app.settings import get_settings

router = APIRouter(prefix="/access", tags=["access"])

_NO_STORE = {"Cache-Control": "no-store"}


class VerifyRequest(BaseModel):
    turnstile_token: str | None = Field(default=None, max_length=4096)
    challenge: str | None = Field(default=None, max_length=4096)
    solution: int | None = Field(default=None, ge=0)


@router.get("/status", summary="Whether this deployment requires human verification")
def status(request: Request) -> JSONResponse:
    settings = get_settings()
    required = bool(settings.public_access)
    verified = (not required) or cookie_payload(
        request.cookies.get(settings.gate_cookie_name, ""), settings
    ) is not None
    payload: dict[str, Any] = {
        "required": required,
        "verified": verified,
        "public_demo": bool(settings.public_demo),
        "turnstile_site_key": settings.turnstile_site_key or "",
        "pow_difficulty": settings.gate_pow_difficulty,
        "pow_algorithm": "sha256-leading-zero-bits",
        "capabilities": public_capabilities(settings),
    }

    visitor_token: str | None = None
    if settings.public_access:
        visitor = visitor_id(request, settings)
        if visitor is None:
            # The status call is the first request every browser makes; this is
            # the one place a stable anonymous quota key can be introduced
            # without requiring login.
            visitor_token, visitor = issue_visitor(settings)
        payload["quota"] = quota_status(request, settings, visitor=visitor)

    response = JSONResponse(content=payload, headers=_NO_STORE)
    if visitor_token is not None:
        set_visitor_cookie(response, visitor_token, settings)
    return response


@router.get("/challenge", summary="Issue a signed proof-of-work challenge")
def challenge(request: Request) -> JSONResponse:
    settings = get_settings()
    if not settings.public_access:
        return JSONResponse(content={"required": False}, headers=_NO_STORE)
    if cookie_payload(request.cookies.get(settings.gate_cookie_name, ""), settings):
        return JSONResponse(
            content={"required": True, "verified": True}, headers=_NO_STORE
        )
    return JSONResponse(content=issue_challenge(settings), headers=_NO_STORE)


@router.post("/verify", summary="Verify Turnstile or proof of work and set the gate cookie")
async def verify(request: Request, payload: VerifyRequest) -> JSONResponse:
    settings = get_settings()
    if not settings.public_access:
        return JSONResponse(content={"ok": True, "required": False}, headers=_NO_STORE)

    if cookie_payload(request.cookies.get(settings.gate_cookie_name, ""), settings):
        return JSONResponse(
            content={"ok": True, "verified": True, "required": True}, headers=_NO_STORE
        )

    if payload.turnstile_token:
        if not settings.turnstile_secret_key:
            raise AccessDenied("turnstile_failed", status_code=400)
        verified = await verify_turnstile(
            payload.turnstile_token, client_ip(request), settings
        )
        if not verified:
            raise AccessDenied("turnstile_failed", status_code=400)
    else:
        if not payload.challenge or payload.solution is None:
            raise AccessDenied("pow_invalid", status_code=400)
        data = verify_challenge(payload.challenge, settings)
        nonce = str(data.get("nonce") or "")
        difficulty = int(data.get("difficulty") or 0)
        if not verify_pow(nonce, payload.solution, difficulty):
            raise AccessDenied("pow_invalid", status_code=400)
        if not consume_nonce(nonce, float(data.get("exp") or 0)):
            raise AccessDenied("challenge_used", status_code=409)

    token = issue_cookie(settings)
    response = JSONResponse(
        content={
            "ok": True,
            "required": True,
            "expires_in": settings.gate_cookie_ttl_seconds,
        },
        headers=_NO_STORE,
    )
    set_gate_cookie(response, token, settings)
    return response


__all__ = ["router"]
