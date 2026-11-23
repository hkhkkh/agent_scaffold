"""Anonymous visitor quotas and token accounting.

The public demo has no accounts. A gate cookie proves "a human checked in", not
"this is person X", so a per-user quota has to be built from two weaker
identities at once:

* a signed, long-lived **visitor cookie** (stable across requests), and
* the client IP, with IPv6 grouped by ``/64``.

Both counters are enforced. Clearing the visitor cookie therefore falls back to
the IP quota, while several people behind one NAT share the IP quota but each
still has their own visitor allowance. Neither is an authentication boundary;
the goal is to stop casual and scripted abuse, not to identify anyone.

The counters live in ``<DATA_DIR>/quotas.db`` (SQLite), not in process memory,
so restarting the backend does not hand everyone a fresh budget. The public
backend is deliberately single-worker, but SQLite also keeps the counts valid
if that ever changes.

Token accounting is wired at the model gateway: every completed model call
reports its usage here. A request gets a :class:`QuotaContext` in a contextvar
for the duration of its handler, so streaming/background work inherits it. The
context is a no-op outside public mode.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.runtime.ai.gateway import UsageLimitExceeded
from app.security.gate import decode_token, encode_token
from app.security.policy import (
    AccessDenied,
    language_for,
    limiter_key,
    message,
)
from app.settings import Settings, get_settings

logger = logging.getLogger("app.security.quotas")

_VISITOR_PURPOSE = "visitor.v1."
#: Providers normally report usage. When they do not, charging zero would make
#: the quota free for exactly the models where it is hardest to measure, so an
#: unknown call is charged a conservative flat amount.
_UNKNOWN_CALL_TOKENS = 1000


class QuotaExceeded(UsageLimitExceeded, AccessDenied):
    """A 429 with a stable code the UI can show as "daily allowance used".

    It also inherits the runtime-neutral marker so an LLM node's streaming
    fallback can re-raise it instead of retrying the same exhausted call.
    """

    def __init__(self, *, retry_after: int = 3600) -> None:
        AccessDenied.__init__(
            self,
            "quota_exceeded",
            status_code=429,
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )


# ---------------------------------------------------------------------------
# Visitor identity
# ---------------------------------------------------------------------------


def issue_visitor(settings: Settings | None = None) -> tuple[str, str]:
    """Return ``(cookie_value, visitor_id)``. The id is not a secret."""
    settings = settings or get_settings()
    now = int(time.time())
    visitor = secrets.token_urlsafe(18)
    payload = {
        "v": 1,
        "vid": visitor,
        "iat": now,
        "exp": now + settings.visitor_cookie_ttl_seconds,
    }
    token = f"v1.{encode_token(_VISITOR_PURPOSE, payload, settings.resolved_gate_secret)}"
    return token, visitor


def visitor_payload(token: str, settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    if not token or not token.startswith("v1."):
        return None
    payload = decode_token(token[3:], _VISITOR_PURPOSE, settings.resolved_gate_secret)
    if payload is None or not isinstance(payload.get("vid"), str):
        return None
    return payload


def visitor_id(request: Request, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    payload = visitor_payload(
        request.cookies.get(settings.visitor_cookie_name, ""), settings
    )
    return str(payload["vid"]) if payload else None


def set_visitor_cookie(
    response: Response, token: str, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    secure = bool(settings.gate_cookie_secure and settings.public_origin_is_https)
    response.set_cookie(
        key=settings.visitor_cookie_name,
        value=token,
        max_age=settings.visitor_cookie_ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


# ---------------------------------------------------------------------------
# Persistent counters
# ---------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _resets_at() -> str:
    tomorrow = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    return tomorrow.isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuotaStore:
    """Tiny SQLite counter store: one row per ``(day, subject)``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            with con:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage (
                        day TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        ai_requests INTEGER NOT NULL DEFAULT 0,
                        runs INTEGER NOT NULL DEFAULT 0,
                        tokens INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (day, subject)
                    )
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10.0)

    def snapshot(self, subject: str) -> dict[str, int]:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT ai_requests, runs, tokens FROM usage WHERE day = ? AND subject = ?",
                (_today(), subject),
            ).fetchone()
        if row is None:
            return {"ai_requests": 0, "runs": 0, "tokens": 0}
        return {"ai_requests": int(row[0]), "runs": int(row[1]), "tokens": int(row[2])}

    def consume_request(self, kind: str, limits: dict[str, int]) -> None:
        """Atomically increment the request counter for every subject.

        The limits are checked inside the transaction, so two concurrent AI
        requests cannot both observe "one left" and both proceed.
        """
        if not limits:
            return
        column = "ai_requests" if kind == "ai" else "runs"
        day = _today()
        with closing(self._connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            for subject, limit in limits.items():
                row = con.execute(
                    f"SELECT {column} FROM usage WHERE day = ? AND subject = ?",
                    (day, subject),
                ).fetchone()
                used = int(row[0]) if row else 0
                if used >= limit:
                    con.rollback()
                    raise QuotaExceeded()
            for subject in limits:
                row = con.execute(
                    "SELECT ai_requests, runs FROM usage WHERE day = ? AND subject = ?",
                    (day, subject),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO usage (day, subject, ai_requests, runs, tokens, updated_at) "
                        "VALUES (?, ?, ?, ?, 0, ?)",
                        (
                            day,
                            subject,
                            1 if kind == "ai" else 0,
                            1 if kind == "run" else 0,
                            _now(),
                        ),
                    )
                else:
                    con.execute(
                        f"UPDATE usage SET {column} = {column} + 1, updated_at = ? "
                        "WHERE day = ? AND subject = ?",
                        (_now(), day, subject),
                    )
            con.commit()

    def add_tokens(
        self,
        limits: dict[str, int],
        amount: int,
        *,
        global_limit: int = 0,
    ) -> None:
        """Add tokens to every subject and to the global row.

        The charge is committed before a limit is raised: the call really did
        spend those tokens, so the ledger must show it. Subsequent calls are
        refused by :meth:`check_headroom`.
        """
        if amount <= 0:
            return
        subjects = dict(limits)
        if global_limit > 0:
            subjects["global"] = global_limit
        over: list[str] = []
        day = _today()
        with closing(self._connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            for subject, limit in subjects.items():
                row = con.execute(
                    "SELECT tokens FROM usage WHERE day = ? AND subject = ?",
                    (day, subject),
                ).fetchone()
                used = int(row[0]) if row else 0
                if used + amount > limit:
                    over.append(subject)
            for subject in subjects:
                row = con.execute(
                    "SELECT tokens FROM usage WHERE day = ? AND subject = ?",
                    (day, subject),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO usage (day, subject, ai_requests, runs, tokens, updated_at) "
                        "VALUES (?, ?, 0, 0, ?, ?)",
                        (day, subject, amount, _now()),
                    )
                else:
                    con.execute(
                        "UPDATE usage SET tokens = tokens + ?, updated_at = ? "
                        "WHERE day = ? AND subject = ?",
                        (amount, _now(), day, subject),
                    )
            con.commit()
        if over:
            raise QuotaExceeded()

    def check_headroom(
        self, limits: dict[str, int], *, global_limit: int = 0
    ) -> None:
        """Refuse to start work that already has no budget left."""
        day = _today()
        with closing(self._connect()) as con:
            for subject, limit in limits.items():
                row = con.execute(
                    "SELECT tokens FROM usage WHERE day = ? AND subject = ?",
                    (day, subject),
                ).fetchone()
                if row is not None and int(row[0]) >= limit:
                    raise QuotaExceeded()
            if global_limit > 0:
                row = con.execute(
                    "SELECT tokens FROM usage WHERE day = ? AND subject = 'global'",
                    (day,),
                ).fetchone()
                if row is not None and int(row[0]) >= global_limit:
                    raise QuotaExceeded()

    def clear(self) -> None:
        """Test helper: remove every counter."""
        with closing(self._connect()) as con, con:
            con.execute("DELETE FROM usage")


_STORES: dict[str, QuotaStore] = {}


def get_quota_store(settings: Settings | None = None) -> QuotaStore:
    settings = settings or get_settings()
    path = (settings.data_dir / "quotas.db").resolve()
    key = str(path)
    store = _STORES.get(key)
    if store is None:
        store = QuotaStore(path)
        _STORES[key] = store
    return store


def reset_quota_store() -> None:
    """Test helper: drop cached stores so a new DATA_DIR is picked up."""
    _STORES.clear()


# ---------------------------------------------------------------------------
# Per-request context and token accounting
# ---------------------------------------------------------------------------


@dataclass
class QuotaContext:
    token_limits: dict[str, int]
    global_token_limit: int
    request_token_limit: int
    tokens: int = 0


_CURRENT: ContextVar[QuotaContext | None] = ContextVar(
    "scaffold_quota_context", default=None
)


def current_quota() -> QuotaContext | None:
    return _CURRENT.get()


def _usage_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    if isinstance(usage, dict):
        values = usage
    else:
        values = {
            "total_tokens": getattr(usage, "total_tokens", None),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        }
    total = values.get("total_tokens")
    if total is None:
        prompt = values.get("prompt_tokens") or 0
        completion = values.get("completion_tokens") or 0
        total = int(prompt) + int(completion)
    try:
        return max(0, int(total or 0))
    except (TypeError, ValueError):
        return 0


def record_usage(usage: Any) -> None:
    """Charge one model call to the request's visitor/IP/global budgets."""
    context = _CURRENT.get()
    if context is None:
        return
    amount = _usage_tokens(usage)
    if amount <= 0:
        amount = _UNKNOWN_CALL_TOKENS
    context.tokens += amount
    get_quota_store().add_tokens(
        context.token_limits,
        amount,
        global_limit=context.global_token_limit,
    )
    if context.request_token_limit > 0 and context.tokens > context.request_token_limit:
        raise QuotaExceeded()


# ---------------------------------------------------------------------------
# Middleware and status
# ---------------------------------------------------------------------------


def _kind(request: Request) -> str | None:
    if request.method.upper() != "POST":
        return None
    path = request.url.path.rstrip("/")
    if path.startswith("/api/ai/"):
        return "ai"
    if path.startswith("/api/runs"):
        # The editor's Run button posts to /api/runs/adhoc and node testing uses
        # /api/runs/node-test. Counting only bare /api/runs let the actual UI
        # bypass both the run allowance and runtime token accounting.
        if path.endswith("/resume"):
            # A resume continues an already-counted run: charge its model calls,
            # but do not spend a second run slot.
            return "resume"
        if path.endswith("/cancel"):
            return None
        return "run"
    return None


def _limits_for(
    settings: Settings, kind: str, visitor: str | None, ip: str
) -> tuple[dict[str, int], dict[str, int]]:
    if kind == "ai":
        visitor_limit = settings.rate_limit_ai_per_day_per_visitor
        ip_limit = settings.rate_limit_ai_per_day_per_ip
        token_limit = settings.visitor_daily_tokens if visitor else settings.ip_daily_tokens
        ip_token_limit = settings.ip_daily_tokens
    else:
        visitor_limit = settings.rate_limit_runs_per_day_per_visitor
        ip_limit = settings.rate_limit_runs_per_day_per_ip
        token_limit = settings.visitor_daily_tokens if visitor else settings.ip_daily_tokens
        ip_token_limit = settings.ip_daily_tokens

    limits = {f"ip:{ip}": ip_limit}
    if visitor:
        limits[f"v:{visitor}"] = visitor_limit
    tokens = {f"ip:{ip}": ip_token_limit}
    if visitor:
        tokens[f"v:{visitor}"] = token_limit
    return limits, tokens


class QuotaMiddleware(BaseHTTPMiddleware):
    """Enforce per-visitor/IP daily request and token allowances.

    This sits *inside* :class:`~app.security.gate.GateMiddleware`, so an
    unverified request never consumes a visitor's allowance. It is a no-op
    outside public mode.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        settings = get_settings()
        if not settings.public_access:
            return await call_next(request)
        kind = _kind(request)
        if kind is None:
            return await call_next(request)
        if getattr(request.state, "admin", False) or getattr(
            request.state, "gate_bypass", False
        ):
            return await call_next(request)

        visitor = visitor_id(request, settings)
        ip = limiter_key(request)
        request_limits, token_limits = _limits_for(settings, kind, visitor, ip)
        context = QuotaContext(
            token_limits=token_limits,
            global_token_limit=settings.global_daily_tokens,
            request_token_limit=(
                settings.ai_request_token_limit if kind == "ai" else 0
            ),
        )
        try:
            store = get_quota_store(settings)
            store.check_headroom(token_limits, global_limit=settings.global_daily_tokens)
            if kind != "resume":
                store.consume_request(kind, request_limits)
        except AccessDenied as exc:
            detail = exc.detail or message(exc.code, language_for(request))
            return JSONResponse(
                status_code=exc.status_code,
                content={"code": exc.code, "detail": detail},
                headers=exc.headers,
            )

        token = _CURRENT.set(context)
        try:
            return await call_next(request)
        finally:
            _CURRENT.reset(token)


def quota_status(
    request: Request,
    settings: Settings | None = None,
    *,
    visitor: str | None = None,
) -> dict[str, Any] | None:
    """Return the visitor's remaining daily allowance for the access UI."""
    settings = settings or get_settings()
    if not settings.public_access:
        return None
    visitor = visitor or visitor_id(request, settings)
    subject = f"v:{visitor}" if visitor else f"ip:{limiter_key(request)}"
    row = get_quota_store(settings).snapshot(subject)

    ai_limit = (
        settings.rate_limit_ai_per_day_per_visitor
        if visitor
        else settings.rate_limit_ai_per_day_per_ip
    )
    run_limit = (
        settings.rate_limit_runs_per_day_per_visitor
        if visitor
        else settings.rate_limit_runs_per_day_per_ip
    )
    token_limit = (
        settings.visitor_daily_tokens if visitor else settings.ip_daily_tokens
    )

    def bucket(used: int, limit: int) -> dict[str, int]:
        return {
            "used": max(0, used),
            "limit": max(0, limit),
            "remaining": max(0, limit - used),
        }

    return {
        "enabled": True,
        "per": "visitor" if visitor else "ip",
        "ai_requests": bucket(row["ai_requests"], ai_limit),
        "runs": bucket(row["runs"], run_limit),
        "tokens": bucket(row["tokens"], token_limit),
        "resets_at": _resets_at(),
    }


__all__ = [
    "QuotaContext",
    "QuotaExceeded",
    "QuotaMiddleware",
    "QuotaStore",
    "current_quota",
    "get_quota_store",
    "issue_visitor",
    "quota_status",
    "record_usage",
    "reset_quota_store",
    "set_visitor_cookie",
    "visitor_id",
    "visitor_payload",
]
