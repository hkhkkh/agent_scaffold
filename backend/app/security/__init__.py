"""Security primitives: authentication, authorization, and the audit trail."""

from app.security.auth import (
    AUTH_MODES,
    AuthError,
    LOCAL_PRINCIPAL,
    Principal,
    PUBLIC_PATHS,
    PUBLIC_PREFIXES,
    SCOPE_ORDER,
    TokenRecord,
    describe_posture,
    generate_token,
    hash_token,
    is_loopback,
    parse_tokens,
    resolve_auth_mode,
    salt_from_env,
    verify_token,
)

__all__ = [
    "AUTH_MODES",
    "AuthError",
    "LOCAL_PRINCIPAL",
    "PUBLIC_PATHS",
    "PUBLIC_PREFIXES",
    "Principal",
    "SCOPE_ORDER",
    "TokenRecord",
    "describe_posture",
    "generate_token",
    "hash_token",
    "is_loopback",
    "parse_tokens",
    "resolve_auth_mode",
    "salt_from_env",
    "verify_token",
]
