"""In-process rate limits for the public deployment.

The public backend runs exactly one uvicorn worker, so a dictionary of sliding
windows is the whole implementation: no Redis, no new dependency, and no
cross-process consistency to get wrong. Restarting the process resets the
windows, which is acceptable for a demo and is documented.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.security.policy import language_for, limiter_key, message
from app.settings import get_settings


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        bucket = self._events.setdefault(key, deque())
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry = max(1, int(bucket[0] + window - now) + 1)
            return False, retry
        bucket.append(now)
        return True, 0

    def clear(self) -> None:
        self._events.clear()


class GlobalDailyLimiter:
    def __init__(self) -> None:
        self._day = ""
        self._count = 0

    def check(self, limit: int) -> tuple[bool, int]:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._day:
            self._day = today
            self._count = 0
        if self._count >= limit:
            tomorrow = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            retry = max(1, int((tomorrow - datetime.now(timezone.utc)).total_seconds()))
            return False, retry
        self._count += 1
        return True, 0

    def clear(self) -> None:
        self._day = ""
        self._count = 0


_limiter = SlidingWindowRateLimiter()
_ai_daily = GlobalDailyLimiter()


def reset_rate_limits() -> None:
    _limiter.clear()
    _ai_daily.clear()


def classify(path: str, method: str) -> str:
    if path.startswith("/api/access/verify"):
        return "verify"
    if path.startswith("/api/access/"):
        return "access"
    if path.startswith("/api/ai/") and method.upper() == "POST":
        return "ai"
    if path.startswith("/api/runs") and method.upper() == "POST":
        return "run"
    if path.startswith("/api/hooks/"):
        return "hook"
    return "general"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        if not settings.public_access:
            return await call_next(request)

        if request.method.upper() == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path in ("/api/health", "/health"):
            return await call_next(request)
        if getattr(request.state, "admin", False) or getattr(
            request.state, "gate_bypass", False
        ):
            return await call_next(request)

        kind = classify(path, request.method)
        limits = {
            "access": settings.rate_limit_access_per_minute,
            "verify": settings.rate_limit_verify_per_minute,
            "ai": settings.rate_limit_ai_per_minute,
            "run": settings.rate_limit_runs_per_minute,
            "hook": settings.rate_limit_hooks_per_minute,
            "general": settings.rate_limit_general_per_minute,
        }
        key = limiter_key(request)
        allowed, retry = _limiter.check(f"{kind}:{key}", limits[kind], 60.0)

        if allowed and kind == "run":
            allowed, retry = _limiter.check(
                f"run-day:{key}", settings.rate_limit_runs_per_day_per_ip, 86_400.0
            )

        if allowed and kind == "ai":
            allowed, retry = _ai_daily.check(settings.rate_limit_ai_per_day_global)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "code": "rate_limited",
                    "detail": message("rate_limited", language_for(request)),
                },
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)


__all__ = [
    "GlobalDailyLimiter",
    "RateLimitMiddleware",
    "SlidingWindowRateLimiter",
    "classify",
    "reset_rate_limits",
]
