"""Authentication and authorization.

Design constraints
------------------

The platform has to run three ways — on one laptop, on a LAN, and on the public
internet — and the same code has to be safe in all three without the operator
remembering to change a flag. That leads to one rule, which the rest of this
module exists to enforce:

    **Authentication is off only when it is provably unnecessary.**

"It is unnecessary" has exactly one meaning here: the server is bound to a
loopback address, so the only reachable client is the machine it runs on. Bind to
anything else and authentication turns itself on, whether or not the operator
thought about it. ``AUTH_MODE`` can opt *into* authentication early, but it
cannot opt *out* of it while listening on a public interface — see
:func:`resolve_auth_mode`.

Tokens
------

Opaque bearer tokens, never JWTs. The reasons are practical rather than
ideological: there is no third party to verify a signature, revocation matters
more than statelessness for a self-hosted tool, and an opaque token lets the
server keep a hash instead of a credential.

Stored as ``sha256(salt || token)``. Plain SHA-256 would be fine for a
high-entropy random token, but operators will paste human-chosen strings, so the
salt matters. Comparison is constant-time, because a token check that leaks
timing is a token check that can be brute-forced one character at a time.

Scopes
------

Three, and deliberately no more:

``read``
    Read workflows, runs, catalogues.
``write``
    Create, edit, delete, and run. Implies ``read``.
``admin``
    Settings, including the model credentials. Implies ``write``.

Anything finer would be speculative. The line that actually matters is
``admin``: it is the scope that can redirect the API key to an attacker's
endpoint, so it is separate from the one that can merely run a workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from typing import Iterable

#: Ordered from least to most privileged. A scope implies every scope below it.
SCOPE_ORDER: tuple[str, ...] = ("read", "write", "admin")

#: Paths reachable without a token. Kept as short as possible: a health check is
#: genuinely useful to a load balancer, and it discloses nothing.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/health",
        "/api/access/status",
        "/api/access/challenge",
        "/api/access/verify",
    }
)

#: Prefixes reachable without a token. Exists for exactly one case: an inbound
#: webhook path carries its own credential in the URL, so it cannot be listed
#: literally in :data:`PUBLIC_PATHS`. Kept as a tuple of spelled-out strings rather
#: than a regex or a predicate so that the complete set of unauthenticated surface
#: is still readable in one place — which is the only property that makes this kind
#: of exemption auditable at all.
#:
#: What guards it instead: the token is 256 bits from `secrets.token_urlsafe`, and
#: `app/api/hooks.py` answers 404 — not 403 — for a token that exists but is
#: disabled, so a probe cannot tell a real hook from a guess that missed.
PUBLIC_PREFIXES: tuple[str, ...] = ("/api/hooks/",)

#: Read-only methods. Used to decide whether `read` is sufficient.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

AUTH_MODES = ("local", "token")


class AuthError(Exception):
    """Raised for configuration that would leave the server unprotected."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    name: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    #: True when authentication was disabled because the bind is loopback-only.
    local: bool = False

    def has(self, scope: str) -> bool:
        """Whether this principal holds ``scope``, honouring the implication chain."""
        if self.local:
            return True
        if scope not in SCOPE_ORDER:
            return False
        highest = max(
            (SCOPE_ORDER.index(s) for s in self.scopes if s in SCOPE_ORDER),
            default=-1,
        )
        return highest >= SCOPE_ORDER.index(scope)

    def required_scope(self, method: str, path: str) -> str:
        """The least privilege that suffices for this request.

        Settings are ``admin`` because that is where the LLM credentials live —
        an operator who can rewrite ``base_url`` can have the API key sent
        anywhere, which is a bigger power than running a workflow.
        """
        if path.startswith("/api/settings"):
            return "admin"
        if (method or "GET").upper() in _SAFE_METHODS:
            return "read"
        return "write"


#: An unauthenticated caller, used in `local` mode. Holds every scope because in
#: that mode the operator *is* the only possible caller.
LOCAL_PRINCIPAL = Principal(name="local", scopes=frozenset(SCOPE_ORDER), local=True)


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


def hash_token(token: str, salt: str = "") -> str:
    """Hash a token for storage and comparison.

    A salt is not defending against a rainbow table over random 32-byte tokens —
    it is defending against the operator who chose ``hunter2``. Mixed in with the
    token rather than stored alongside, so the digest alone is not enough to
    check a guess offline.
    """
    material = f"{salt}:{token}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def verify_token(presented: str, expected_hash: str, salt: str = "") -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    if not presented or not expected_hash:
        return False
    candidate = hash_token(presented, salt)
    return hmac.compare_digest(candidate, expected_hash)


def generate_token(prefix: str = "ask") -> str:
    """A fresh token, long enough that guessing is not a strategy."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class TokenRecord:
    """One configured principal and the hash of its token."""

    name: str
    token_hash: str
    scopes: frozenset[str]

    @classmethod
    def from_spec(cls, spec: str, salt: str = "") -> TokenRecord:
        """Parse ``name:token:scope|scope``.

        Parsed strictly and eagerly: a typo in a security configuration should
        stop the server at startup, not quietly produce a token nobody can use or
        — far worse — a principal with more scope than intended.
        """
        parts = spec.split(":")
        if len(parts) < 2:
            raise AuthError(
                f"Malformed token entry {spec!r}. Expected 'name:token' or "
                "'name:token:scope1|scope2'."
            )
        name = parts[0].strip()
        token = parts[1]
        if not name or not token:
            raise AuthError(f"Malformed token entry {spec!r}: empty name or token.")

        raw_scopes = parts[2] if len(parts) > 2 else "write"
        scopes = frozenset(s.strip() for s in raw_scopes.split("|") if s.strip())
        unknown = scopes - set(SCOPE_ORDER)
        if unknown:
            raise AuthError(
                f"Unknown scope(s) {sorted(unknown)} for '{name}'. "
                f"Known scopes: {', '.join(SCOPE_ORDER)}."
            )
        if not scopes:
            scopes = frozenset({"write"})
        return cls(name=name, token_hash=hash_token(token, salt), scopes=scopes)


def parse_tokens(raw: Iterable[str], salt: str = "") -> list[TokenRecord]:
    records: list[TokenRecord] = []
    seen: set[str] = set()
    for spec in raw:
        spec = (spec or "").strip()
        if not spec:
            continue
        record = TokenRecord.from_spec(spec, salt)
        if record.name in seen:
            raise AuthError(f"Duplicate principal name {record.name!r}.")
        seen.add(record.name)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

#: Hosts that mean "only this machine can reach it".
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "127.0.0.2"})


def is_loopback(host: str) -> bool:
    """Whether ``host`` confines the server to the local machine.

    Deliberately conservative: ``0.0.0.0`` and ``::`` are *not* loopback, because
    binding them exposes the port on every interface. Treating them as local is
    the single most common way a self-hosted tool ends up unauthenticated on a
    network.
    """
    candidate = (host or "").strip().lower().strip("[]")
    if candidate in _LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 is entirely loopback.
    return candidate.startswith("127.")


def resolve_auth_mode(configured: str, host: str) -> str:
    """Decide the effective mode, refusing to be insecure by accident.

    ``local`` is only honoured when the server is bound to loopback. An operator
    who set ``AUTH_MODE=local`` and then bound ``0.0.0.0`` almost certainly did
    not mean to publish an unauthenticated control plane that can execute
    arbitrary Python, so the request is escalated to ``token`` rather than
    obeyed.

    The escalation is deliberate and one-directional: configuration may make the
    server *more* secure than asked, never less.
    """
    mode = (configured or "").strip().lower() or "local"
    if mode not in AUTH_MODES:
        raise AuthError(
            f"Unknown AUTH_MODE {configured!r}. Expected one of: {', '.join(AUTH_MODES)}."
        )
    if mode == "local" and not is_loopback(host):
        return "token"
    return mode


def needs_token_list(mode: str) -> bool:
    return mode == "token"


def describe_posture(mode: str, host: str, records: int) -> str:
    """One line describing the security posture, for the startup log.

    Worth printing every boot: an operator who cannot see which mode is active
    will not notice when it changes.
    """
    if mode == "local":
        return (
            f"auth=local — no token required because the server is bound to "
            f"{host} (loopback only). Bind elsewhere to require authentication."
        )
    return f"auth=token — bearer token required on /api/* ({records} principal(s))."


def salt_from_env() -> str:
    """Salt for token hashing.

    An explicitly configured salt is used when present. Otherwise a stable
    per-installation value is derived so that restarts do not invalidate tokens;
    a random per-boot salt would silently log everyone out on every deploy.
    """
    configured = os.environ.get("AUTH_SALT", "").strip()
    if configured:
        return configured
    return "agent-scaffold-default-salt"


__all__ = [
    "AUTH_MODES",
    "AuthError",
    "LOCAL_PRINCIPAL",
    "Principal",
    "PUBLIC_PATHS",
    "SCOPE_ORDER",
    "TokenRecord",
    "describe_posture",
    "generate_token",
    "hash_token",
    "is_loopback",
    "needs_token_list",
    "parse_tokens",
    "resolve_auth_mode",
    "salt_from_env",
    "verify_token",
]
