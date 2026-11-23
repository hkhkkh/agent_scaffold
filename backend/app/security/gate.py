"""Signed human-verification gate.

Two locks, one door:

* a Cloudflare Turnstile check when the operator configured one, and
* a server-signed proof-of-work challenge that works without a third party.

The PoW fallback exists because this deployment is used from mainland China,
where ``challenges.cloudflare.com`` can be slow or unreachable. It is a speed
bump, not a bot detector; the real protection is the rate limiter and the
Cloudflare edge. The callback either way is a short-lived signed cookie.

Everything here is inert unless ``PUBLIC_ACCESS=true``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.security.policy import (
    AccessDenied,
    GATE_PUBLIC_PATHS,
    GATE_PUBLIC_PREFIXES,
    denied_response,
)
from app.settings import Settings, get_settings

logger = logging.getLogger("app.security.gate")

_COOKIE_PURPOSE = "cookie.v1."
_CHALLENGE_PURPOSE = "challenge.v1."
_ALGORITHM = "sha256-leading-zero-bits"

#: Nonces are single-use. The store is process-local, which is why the public
#: deployment runs exactly one worker. Entries are pruned by their own expiry.
_used_nonces: dict[str, float] = {}


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(secret: str, purpose: str, raw: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), purpose.encode("utf-8") + raw, hashlib.sha256)
    return _b64encode(digest.digest())


def encode_token(purpose: str, payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64encode(raw)}.{_sign(secret, purpose, raw)}"


def decode_token(token: str, purpose: str, secret: str) -> dict[str, Any] | None:
    try:
        payload_part, signature = token.rsplit(".", 1)
        raw = _b64decode(payload_part)
        expected = _sign(secret, purpose, raw)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    if float(payload.get("exp") or 0) <= time.time():
        return None
    return payload


def issue_cookie(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = int(time.time())
    payload = {
        "v": 1,
        "iat": now,
        "exp": now + settings.gate_cookie_ttl_seconds,
        "jti": secrets.token_urlsafe(18),
    }
    return f"v1.{encode_token(_COOKIE_PURPOSE, payload, settings.resolved_gate_secret)}"


def cookie_payload(token: str, settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    if not token or not token.startswith("v1."):
        return None
    return decode_token(token[3:], _COOKIE_PURPOSE, settings.resolved_gate_secret)


def verify_cookie(token: str, settings: Settings | None = None) -> bool:
    return cookie_payload(token, settings) is not None


def set_gate_cookie(response: Response, token: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    secure = bool(settings.gate_cookie_secure and settings.public_origin_is_https)
    response.set_cookie(
        key=settings.gate_cookie_name,
        value=token,
        max_age=settings.gate_cookie_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def issue_challenge(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    now = int(time.time())
    nonce = secrets.token_urlsafe(32)
    payload = {
        "v": 1,
        "iat": now,
        "exp": now + settings.gate_challenge_ttl_seconds,
        "nonce": nonce,
        "difficulty": settings.gate_pow_difficulty,
    }
    challenge = encode_token(_CHALLENGE_PURPOSE, payload, settings.resolved_gate_secret)
    return {
        "required": True,
        "challenge": challenge,
        "nonce": nonce,
        "difficulty": settings.gate_pow_difficulty,
        "expires_in": settings.gate_challenge_ttl_seconds,
        "algorithm": _ALGORITHM,
    }


def verify_challenge(challenge: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    payload = decode_token(challenge, _CHALLENGE_PURPOSE, settings.resolved_gate_secret)
    if payload is None:
        raise AccessDenied("pow_invalid", status_code=400)
    difficulty = int(payload.get("difficulty") or 0)
    if difficulty < 12 or difficulty > 22:
        raise AccessDenied("pow_invalid", status_code=400)
    return payload


def leading_zero_bits(data: bytes) -> int:
    bits = 0
    for byte in data:
        if byte == 0:
            bits += 8
            continue
        for shift in range(7, -1, -1):
            if byte & (1 << shift):
                return bits
            bits += 1
        return bits
    return bits


def verify_pow(nonce: str, solution: int, difficulty: int) -> bool:
    if not nonce or solution < 0 or difficulty < 1:
        return False
    digest = hashlib.sha256(f"{nonce}:{solution}".encode("utf-8")).digest()
    return leading_zero_bits(digest) >= difficulty


def consume_nonce(nonce: str, expires_at: float) -> bool:
    """Mark a nonce used. Returns False when it was already used."""
    now = time.time()
    for key, expiry in list(_used_nonces.items()):
        if expiry <= now:
            _used_nonces.pop(key, None)
    if nonce in _used_nonces:
        return False
    _used_nonces[nonce] = max(float(expires_at), now + 1)
    return True


def reset_nonce_store() -> None:
    _used_nonces.clear()


async def verify_turnstile(
    token: str,
    remote_ip: str,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    if not token or not settings.turnstile_secret_key:
        return False
    data = {
        "secret": settings.turnstile_secret_key,
        "response": token,
    }
    if remote_ip and remote_ip != "unknown":
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=settings.turnstile_timeout_seconds) as client:
            response = await client.post(settings.turnstile_verify_url, data=data)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # The token is a credential-shaped value; never log it. The error type is
        # enough to distinguish "Cloudflare unreachable" from "token rejected".
        logger.warning("Turnstile verification failed: %s", type(exc).__name__)
        return False

    if not isinstance(payload, dict) or payload.get("success") is not True:
        return False
    if payload.get("action") != "scaffold-gate":
        return False
    expected_host = settings.public_origin_host
    if expected_host and str(payload.get("hostname") or "").lower() != expected_host:
        return False
    return True


class GateMiddleware(BaseHTTPMiddleware):
    """Require a signed gate cookie for every API route except the gate itself."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        if not settings.public_access:
            return await call_next(request)

        path = request.url.path
        if (
            not path.startswith("/api")
            or path in GATE_PUBLIC_PATHS
            or path.startswith(GATE_PUBLIC_PREFIXES)
        ):
            return await call_next(request)

        if getattr(request.state, "gate_bypass", False) or getattr(
            request.state, "admin", False
        ):
            return await call_next(request)

        payload = cookie_payload(
            request.cookies.get(settings.gate_cookie_name, ""), settings
        )
        if payload is not None:
            request.state.gate_verified = True
            request.state.gate_jti = payload.get("jti")
            return await call_next(request)

        return denied_response("gate_required", request)


__all__ = [
    "GateMiddleware",
    "consume_nonce",
    "cookie_payload",
    "decode_token",
    "encode_token",
    "issue_challenge",
    "issue_cookie",
    "leading_zero_bits",
    "reset_nonce_store",
    "set_gate_cookie",
    "verify_challenge",
    "verify_cookie",
    "verify_pow",
    "verify_turnstile",
]
