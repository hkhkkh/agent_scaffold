"""Authentication and authorization tests.

The property under test throughout is the same one: **the server is never less
protected than the operator intended, and never protected by accident either.**
Two failure modes matter and both are covered:

* it serves an open control plane to a network (that is what the escalation
  tests are about);
* it locks out a legitimate local user for no reason (that is what the
  local-mode tests are about).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import deps
from app.security.auth import (
    LOCAL_PRINCIPAL,
    AuthError,
    Principal,
    TokenRecord,
    generate_token,
    hash_token,
    is_loopback,
    parse_tokens,
    resolve_auth_mode,
    verify_token,
)
from app.security.deps import AuthMiddleware, configure


@pytest.fixture(autouse=True)
def _reset_auth_state():
    """Every test starts from a known configuration and cleans up after itself."""
    original = deps.get_state()
    yield
    deps._state = original


# ---------------------------------------------------------------------------
# Loopback detection
# ---------------------------------------------------------------------------


class TestLoopbackDetection:
    def test_loopback_addresses_are_recognised(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost", "[::1]"):
            assert is_loopback(host), host

    def test_whole_127_range_counts_as_loopback(self):
        assert is_loopback("127.99.99.99")

    def test_wildcard_bind_is_not_loopback(self):
        """``0.0.0.0`` and ``::`` expose the port on every interface.

        Treating either as "local" is the single most common way a self-hosted
        tool ends up unauthenticated on a network, so it is asserted explicitly.
        """
        for host in ("0.0.0.0", "::", "", "192.168.1.5", "example.com"):
            assert not is_loopback(host), host


# ---------------------------------------------------------------------------
# Mode resolution — the security-by-default rule
# ---------------------------------------------------------------------------


class TestModeResolution:
    def test_local_mode_is_honoured_on_loopback(self):
        assert resolve_auth_mode("local", "127.0.0.1") == "local"

    def test_local_mode_is_escalated_on_a_network_bind(self):
        """The heart of the design: configuration may tighten, never loosen.

        An operator who asks for `local` while bound to 0.0.0.0 has almost
        certainly not thought about it, and the consequence — an unauthenticated
        endpoint that executes arbitrary Python — is severe enough that obeying
        would be the wrong kind of literal.
        """
        assert resolve_auth_mode("local", "0.0.0.0") == "token"
        assert resolve_auth_mode("local", "192.168.1.10") == "token"

    def test_token_mode_is_never_downgraded(self):
        assert resolve_auth_mode("token", "127.0.0.1") == "token"

    def test_default_is_local(self):
        assert resolve_auth_mode("", "127.0.0.1") == "local"

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(AuthError, match="Unknown AUTH_MODE"):
            resolve_auth_mode("none", "127.0.0.1")

    def test_token_mode_without_tokens_refuses_to_start(self):
        """An open control plane is worse than a failed boot."""
        with pytest.raises(AuthError, match="requires at least one token"):
            configure(mode="token", host="0.0.0.0", token_specs=[])

    def test_escalated_local_without_tokens_also_refuses(self):
        with pytest.raises(AuthError, match="requires at least one token"):
            configure(mode="local", host="0.0.0.0", token_specs=[])


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


class TestTokenHandling:
    def test_hash_is_stable_for_the_same_salt(self):
        assert hash_token("abc", "s") == hash_token("abc", "s")

    def test_hash_changes_with_the_salt(self):
        assert hash_token("abc", "s1") != hash_token("abc", "s2")

    def test_verify_accepts_the_right_token(self):
        assert verify_token("secret", hash_token("secret", "s"), "s")

    def test_verify_rejects_the_wrong_token(self):
        assert not verify_token("wrong", hash_token("secret", "s"), "s")

    def test_verify_rejects_empty_inputs(self):
        assert not verify_token("", hash_token("secret", "s"), "s")
        assert not verify_token("secret", "", "s")

    def test_the_plaintext_never_appears_in_the_hash(self):
        token = "supersecret"
        assert token not in hash_token(token, "s")

    def test_generated_tokens_are_long_and_unique(self):
        tokens = {generate_token() for _ in range(20)}
        assert len(tokens) == 20
        assert all(len(t) > 30 for t in tokens)

    def test_comparison_is_constant_time(self):
        """A timing-leaky comparison can be brute-forced character by character."""
        import inspect

        from app.security import auth

        source = inspect.getsource(auth.verify_token)
        assert "compare_digest" in source, (
            "verify_token must compare with hmac.compare_digest; a plain == is "
            "timing-dependent"
        )


class TestTokenRecord:
    def test_parses_name_and_token(self):
        record = TokenRecord.from_spec("admin:sekrit", "s")
        assert record.name == "admin"
        assert record.token_hash == hash_token("sekrit", "s")

    def test_defaults_to_write_scope(self):
        assert TokenRecord.from_spec("a:t", "s").scopes == frozenset({"write"})

    def test_parses_multiple_scopes(self):
        record = TokenRecord.from_spec("a:t:read|write|admin", "s")
        assert record.scopes == frozenset({"read", "write", "admin"})

    def test_malformed_entries_are_rejected(self):
        for spec in ("justaname", "", ":token", "name:"):
            with pytest.raises(AuthError):
                TokenRecord.from_spec(spec, "s")

    def test_unknown_scope_is_rejected(self):
        """A typo must stop the server, not grant a principal unintended power.

        `read|adminn` silently dropping to `read` would be forgiving in the worst
        direction; refusing is the only safe reading of an ambiguous config.
        """
        with pytest.raises(AuthError, match="Unknown scope"):
            TokenRecord.from_spec("a:t:read|superuser", "s")

    def test_duplicate_names_are_rejected(self):
        with pytest.raises(AuthError, match="Duplicate principal"):
            parse_tokens(["a:t1", "a:t2"], "s")

    def test_blank_entries_are_skipped(self):
        assert len(parse_tokens(["a:t", "", "   "], "s")) == 1


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


class TestScopes:
    def test_admin_implies_everything(self):
        principal = Principal(name="a", scopes=frozenset({"admin"}))
        assert principal.has("read") and principal.has("write") and principal.has("admin")

    def test_write_implies_read_but_not_admin(self):
        principal = Principal(name="a", scopes=frozenset({"write"}))
        assert principal.has("read") and principal.has("write")
        assert not principal.has("admin")

    def test_read_is_least_privilege(self):
        principal = Principal(name="a", scopes=frozenset({"read"}))
        assert principal.has("read")
        assert not principal.has("write") and not principal.has("admin")

    def test_unknown_scope_is_never_granted(self):
        principal = Principal(name="a", scopes=frozenset({"admin"}))
        assert not principal.has("root")

    def test_local_principal_holds_everything(self):
        assert LOCAL_PRINCIPAL.has("admin")

    def test_settings_requires_admin(self):
        """Settings is where the API key lives, so it is not a `write` action.

        Anyone who can rewrite `base_url` can have the operator's key sent to
        their own endpoint — a bigger power than running a workflow.
        """
        principal = Principal(name="a", scopes=frozenset({"write"}))
        assert principal.required_scope("PUT", "/api/settings/llm") == "admin"
        assert not principal.has("admin")

    def test_read_methods_need_read(self):
        principal = Principal(name="a", scopes=frozenset())
        assert principal.required_scope("GET", "/api/workflows") == "read"
        assert principal.required_scope("HEAD", "/api/workflows") == "read"

    def test_mutating_methods_need_write(self):
        principal = Principal(name="a", scopes=frozenset())
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert principal.required_scope(method, "/api/workflows") == "write"


# ---------------------------------------------------------------------------
# Middleware behaviour
# ---------------------------------------------------------------------------


#: A peer address that looks like a real loopback client. `TestClient` defaults
#: to the synthetic host ``"testclient"``, which is not loopback and is therefore
#: correctly refused in local mode — so the tests have to supply a real address
#: rather than the harness default.
LOOPBACK_PEER = ("127.0.0.1", 54321)
REMOTE_PEER = ("192.168.1.50", 54321)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/workflows")
    def list_workflows():
        return {"workflows": []}

    @app.post("/api/workflows")
    def create_workflow():
        return {"id": "w1"}

    @app.get("/api/settings")
    def settings_read():
        return {"llm": {}}

    @app.get("/")
    def index():
        return {"app": "spa"}

    return app


class TestMiddleware:
    def test_local_mode_allows_without_a_token(self):
        configure(mode="local", host="127.0.0.1")
        client = TestClient(_app(), client=LOOPBACK_PEER)
        assert client.get("/api/workflows").status_code == 200

    def test_local_mode_refuses_a_non_loopback_peer(self):
        """Defence in depth against `SERVER_HOST` disagreeing with the real bind.

        If the operator sets SERVER_HOST=127.0.0.1 while uvicorn was told
        `--host 0.0.0.0`, the mode check alone would wave every remote request
        through. Verifying the peer closes that gap without requiring the two
        settings to be kept in sync by hand.
        """
        configure(mode="local", host="127.0.0.1")
        client = TestClient(_app(), client=REMOTE_PEER)
        response = client.get("/api/workflows")

        assert response.status_code == 401
        assert "local mode" in response.json()["detail"]

    def test_a_remote_peer_still_works_in_token_mode(self):
        """The peer check must not become an accidental LAN ban."""
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app(), client=REMOTE_PEER)
        response = client.get(
            "/api/workflows", headers={"Authorization": "Bearer sekrit"}
        )
        assert response.status_code == 200

    def test_token_mode_rejects_a_missing_token(self):
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        response = client.get("/api/workflows")

        assert response.status_code == 401
        # A challenge is part of the contract; without it a client cannot know
        # what kind of credential to present.
        assert response.headers.get("www-authenticate") == "Bearer"

    def test_token_mode_rejects_a_wrong_token(self):
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        response = client.get("/api/workflows", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_token_mode_accepts_the_right_token(self):
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        response = client.get("/api/workflows", headers={"Authorization": "Bearer sekrit"})
        assert response.status_code == 200

    def test_x_api_key_is_accepted(self):
        """Because a workflow's HTTP tool can set it without header syntax."""
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        assert client.get("/api/workflows", headers={"X-API-Key": "sekrit"}).status_code == 200

    def test_insufficient_scope_is_forbidden_not_unauthorised(self):
        """401 and 403 mean different things and must not be conflated."""
        configure(mode="token", host="0.0.0.0", token_specs=["reader:r3ad:read"])
        client = TestClient(_app())

        assert client.get(
            "/api/workflows", headers={"Authorization": "Bearer r3ad"}
        ).status_code == 200
        assert client.post(
            "/api/workflows", headers={"Authorization": "Bearer r3ad"}
        ).status_code == 403

    def test_read_scope_cannot_reach_settings(self):
        configure(mode="token", host="0.0.0.0", token_specs=["reader:r3ad:read"])
        client = TestClient(_app())
        response = client.get("/api/settings", headers={"Authorization": "Bearer r3ad"})
        assert response.status_code == 403

    def test_admin_scope_can_reach_settings(self):
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        assert client.get(
            "/api/settings", headers={"Authorization": "Bearer sekrit"}
        ).status_code == 200

    def test_health_is_reachable_without_a_token(self):
        """A load balancer has no credential; the endpoint discloses nothing."""
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        assert client.get("/api/health").status_code == 200

    def test_non_api_paths_are_untouched(self):
        """The SPA is not a secret, and gating it would break the first page load."""
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        assert client.get("/").status_code == 200

    def test_preflight_is_not_blocked(self):
        """A browser cannot attach a token to a CORS preflight."""
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        response = client.options("/api/workflows")
        assert response.status_code != 401

    def test_an_empty_bearer_is_rejected(self):
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        assert client.get(
            "/api/workflows", headers={"Authorization": "Bearer "}
        ).status_code == 401

    def test_a_malformed_authorization_header_is_rejected(self):
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        assert client.get(
            "/api/workflows", headers={"Authorization": "sekrit"}
        ).status_code == 401

    def test_a_token_cannot_be_prefix_matched(self):
        """Substring or prefix matching would be a real bypass."""
        configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
        client = TestClient(_app())
        for guess in ("sekr", "sekritx", "sekri", "SEKRIT"):
            assert (
                client.get(
                    "/api/workflows", headers={"Authorization": f"Bearer {guess}"}
                ).status_code
                == 401
            ), guess

    def test_multiple_principals_each_use_their_own_scope(self):
        configure(
            mode="token",
            host="0.0.0.0",
            token_specs=["reader:r3ad:read", "ops:0ps:write"],
        )
        client = TestClient(_app())

        assert client.get(
            "/api/workflows", headers={"Authorization": "Bearer r3ad"}
        ).status_code == 200
        assert client.post(
            "/api/workflows", headers={"Authorization": "Bearer r3ad"}
        ).status_code == 403
        assert client.post(
            "/api/workflows", headers={"Authorization": "Bearer 0ps"}
        ).status_code == 200

    def test_posture_line_reports_the_mode(self):
        state = configure(mode="local", host="127.0.0.1")
        assert "auth=local" in state.posture()

        state = configure(mode="token", host="0.0.0.0", token_specs=["a:t:admin"])
        assert "auth=token" in state.posture()
        assert "1 principal" in state.posture()

    def test_rejections_are_counted(self):
        """A cheap signal that something is probing the API."""
        state = configure(mode="token", host="0.0.0.0", token_specs=["a:t:admin"])
        client = TestClient(_app())
        before = state.rejections
        client.get("/api/workflows")
        client.get("/api/workflows", headers={"Authorization": "Bearer bad"})
        assert deps.get_state().rejections == before + 2


class TestTheRealApplicationIsWired:
    """The unit tests above build their own app, so they cannot prove the real one.

    This class exists because of a gap the negative-validation pass found: every
    middleware test constructed a throwaway ``FastAPI()`` and attached the
    middleware to it. Deleting ``app.add_middleware(AuthMiddleware)`` from
    ``main.py`` left all of them green — a security control that exists, is
    tested, and is not actually installed. The only defence against that class of
    mistake is to assert on the shipped application object itself.
    """

    def test_the_shipped_app_installs_the_auth_middleware(self):
        from app.main import app as shipped
        from app.security.deps import AuthMiddleware

        installed = [m.cls for m in shipped.user_middleware]
        assert AuthMiddleware in installed, (
            "AuthMiddleware is not attached to app.main.app. Every middleware "
            "test would still pass while the API is wide open."
        )

    def test_cors_is_outermost_so_preflight_is_answered(self):
        """Order matters: a token check in front of CORS breaks the browser.

        Starlette wraps in reverse order of `user_middleware`, so a *lower* index
        is a *more outer* layer.
        """
        from fastapi.middleware.cors import CORSMiddleware

        from app.main import app as shipped
        from app.security.deps import AuthMiddleware

        installed = [m.cls for m in shipped.user_middleware]
        assert installed.index(CORSMiddleware) < installed.index(AuthMiddleware), (
            "CORS must be outside Auth, otherwise a preflight (which cannot "
            "carry a token) is rejected before it is answered"
        )

    def test_the_shipped_app_enforces_token_mode(self, monkeypatch):
        """End to end through the real object, not a stand-in."""
        from app.main import app as shipped
        from app.settings import get_settings

        monkeypatch.setenv("AUTH_MODE", "token")
        monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
        monkeypatch.setenv("AUTH_TOKENS", "admin:sekrit:admin")
        get_settings.cache_clear()
        try:
            configure(mode="token", host="0.0.0.0", token_specs=["admin:sekrit:admin"])
            client = TestClient(shipped, client=REMOTE_PEER)

            assert client.get("/api/node-types").status_code == 401
            assert (
                client.get(
                    "/api/node-types",
                    headers={"Authorization": "Bearer sekrit"},
                ).status_code
                == 200
            )
        finally:
            get_settings.cache_clear()
