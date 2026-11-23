"""The public gate's guarantees, independent of the real application wiring.

These use a small FastAPI app so the gate, the demo policy, the rate limiter and
the body cap can be exercised without booting the scheduler or a database.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import access
from app.graph.schema import WorkflowGraph
from app.graph.validate import validate_graph
from app.security import gate as gate_module
from app.security.gate import (
    GateMiddleware,
    cookie_payload,
    issue_challenge,
    issue_cookie,
    leading_zero_bits,
    verify_cookie,
)
from app.security.policy import (
    AccessDenied,
    PublicDemoMiddleware,
    enforce_public_graph_policy,
    language_for,
    message,
    public_capabilities,
    require_admin,
)
from app.security.quotas import reset_quota_store
from app.security.ratelimit import RateLimitMiddleware, reset_rate_limits
from app.security.web import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.settings import Settings, get_settings


PUBLIC_ENV = {
    "PUBLIC_ACCESS": "true",
    "PUBLIC_DEMO": "true",
    "PUBLIC_ORIGIN": "http://testserver",
    "TRUSTED_HOSTS": "testserver",
    "AUTH_MODE": "local",
    "SERVER_HOST": "127.0.0.1",
    "GATE_COOKIE_SECRET": "g" * 40,
    "ADMIN_TOKEN": "a" * 40,
    "GATE_BYPASS_TOKEN": "b" * 40,
    "GATE_POW_DIFFICULTY": "12",
    "CODE_NODE_ENABLED": "false",
    "SCHEDULER_ENABLED": "false",
    "MAX_CONCURRENT_RUNS": "2",
    "MAX_RUN_TOKENS": "1000",
    "MAX_RUN_SECONDS": "10",
    "MAX_RUN_STEPS": "10",
    "MAX_WORKFLOWS": "5",
    "MAX_NODES_PER_WORKFLOW": "5",
    "MAX_EDGES_PER_WORKFLOW": "5",
    "MAX_REQUEST_BYTES": "1024",
    "TURNSTILE_SITE_KEY": "",
    "TURNSTILE_SECRET_KEY": "",
}


@pytest.fixture(autouse=True)
def public_settings(monkeypatch, tmp_path):
    for key, value in PUBLIC_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    gate_module.reset_nonce_store()
    reset_rate_limits()
    reset_quota_store()
    yield
    get_settings.cache_clear()
    gate_module.reset_nonce_store()
    reset_rate_limits()
    reset_quota_store()


def _app(*extra_middleware: object) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AccessDenied)
    async def denied(request: Request, exc: AccessDenied):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "detail": exc.detail or message(exc.code, language_for(request)),
            },
        )

    @app.get("/api/protected")
    def protected():
        return {"ok": True}

    @app.get("/api/settings")
    def settings_route(_admin: None = Depends(require_admin)):
        return {"ok": True}

    @app.post("/api/echo")
    async def echo(request: Request):
        return {"bytes": len(await request.body())}

    app.include_router(access.router, prefix="/api")
    app.add_middleware(GateMiddleware)
    for middleware in extra_middleware:
        app.add_middleware(middleware)  # type: ignore[arg-type]
    app.add_middleware(PublicDemoMiddleware)
    return app


def _solve(nonce: str, difficulty: int) -> int:
    solution = 0
    while True:
        digest = hashlib.sha256(f"{nonce}:{solution}".encode()).digest()
        if leading_zero_bits(digest) >= difficulty:
            return solution
        solution += 1


def _verify(client: TestClient) -> None:
    challenge = client.get("/api/access/challenge").json()
    solution = _solve(challenge["nonce"], challenge["difficulty"])
    response = client.post(
        "/api/access/verify",
        json={"challenge": challenge["challenge"], "solution": solution},
    )
    assert response.status_code == 200, response.text


def test_status_reports_required_and_unverified():
    with TestClient(_app()) as client:
        body = client.get("/api/access/status").json()
    assert body["required"] is True
    assert body["verified"] is False
    assert body["public_demo"] is True
    assert body["capabilities"]["code_nodes"] is False


def test_protected_route_requires_the_cookie():
    with TestClient(_app()) as client:
        response = client.get("/api/protected")
    assert response.status_code == 403
    assert response.json()["code"] == "gate_required"


def test_verify_sets_a_cookie_and_unlocks_the_api():
    with TestClient(_app()) as client:
        _verify(client)
        response = client.get("/api/protected")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        status = client.get("/api/access/status").json()
        assert status["verified"] is True


def test_a_challenge_cannot_be_replayed():
    with TestClient(_app()) as first:
        challenge = first.get("/api/access/challenge").json()
        solution = _solve(challenge["nonce"], challenge["difficulty"])
        assert first.post(
            "/api/access/verify",
            json={"challenge": challenge["challenge"], "solution": solution},
        ).status_code == 200

    with TestClient(_app()) as second:
        response = second.post(
            "/api/access/verify",
            json={"challenge": challenge["challenge"], "solution": solution},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "challenge_used"


def test_a_tampered_cookie_is_rejected():
    with TestClient(_app()) as client:
        _verify(client)
        good = client.cookies.get("scaffold_gate")
        assert good
        client.cookies.set("scaffold_gate", good[:-2] + "xx")
        response = client.get("/api/protected")
    assert response.status_code == 403
    assert response.json()["code"] == "gate_required"


def test_admin_path_needs_the_admin_header_even_after_the_gate():
    with TestClient(_app()) as client:
        _verify(client)
        assert client.get("/api/settings").status_code == 403
        response = client.get("/api/settings", headers={"X-Admin-Token": "a" * 40})
        assert response.status_code == 200


def test_gate_bypass_header_is_an_automation_credential():
    with TestClient(_app()) as client:
        response = client.get(
            "/api/protected", headers={"X-Scaffold-Gate-Bypass": "b" * 40}
        )
    assert response.status_code == 200


def test_body_limit_rejects_a_large_declared_body():
    with TestClient(_app(BodySizeLimitMiddleware)) as client:
        response = client.post("/api/echo", content=b"x" * 2048)
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_rate_limit_returns_429_after_the_window_is_full(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ACCESS_PER_MINUTE", "2")
    get_settings.cache_clear()
    with TestClient(_app(RateLimitMiddleware)) as client:
        assert client.get("/api/access/status").status_code == 200
        assert client.get("/api/access/status").status_code == 200
        response = client.get("/api/access/status")
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_security_headers_are_present():
    with TestClient(_app(SecurityHeadersMiddleware)) as client:
        response = client.get("/api/access/status")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "challenges.cloudflare.com" in response.headers["Content-Security-Policy"]


def test_cookie_helpers_are_purpose_separated():
    settings = get_settings()
    cookie = issue_cookie(settings)
    assert verify_cookie(cookie, settings)
    challenge = issue_challenge(settings)
    assert cookie_payload(challenge["challenge"], settings) is None


def test_public_demo_code_nodes_warn_but_remain_viewable():
    graph = WorkflowGraph.model_validate(
        {
            "id": "demo",
            "name": "demo",
            "nodes": [
                {"id": "s", "type": "start", "label": "Start"},
                {
                    "id": "c",
                    "type": "code",
                    "label": "Code",
                    "trusted": True,
                    "config": {"code": "result = 1"},
                },
                {"id": "e", "type": "end", "label": "End"},
            ],
            "edges": [
                {"id": "s-c", "source": "s", "target": "c"},
                {"id": "c-e", "source": "c", "target": "e"},
            ],
        }
    )
    report = validate_graph(graph)
    codes = [issue.code for issue in report.issues]
    assert "code_node_disabled" in codes
    assert report.ok  # a warning, not an error
    with pytest.raises(AccessDenied):
        enforce_public_graph_policy(graph, get_settings())


def test_public_capabilities_close_admin_surfaces():
    capabilities = public_capabilities(get_settings())
    assert capabilities["settings"] is False
    assert capabilities["schedules"] is False
    assert capabilities["workflow_delete"] is False
    assert capabilities["code_nodes"] is False


def test_validate_public_refuses_example_placeholders():
    settings = Settings(
        public_access=True,
        public_demo=False,
        public_origin="https://demo.example.com",
        admin_token="replace-with-at-least-32-random-characters",
        gate_cookie_secret="g" * 40,
        auth_salt="s" * 40,
        auth_mode="local",
        server_host="127.0.0.1",
    )
    with pytest.raises(ValueError, match="placeholder"):
        settings.validate_public()


def test_validate_public_refuses_code_nodes_in_demo():
    settings = Settings(
        public_access=True,
        public_demo=True,
        public_origin="https://demo.example.com",
        admin_token="a" * 40,
        gate_cookie_secret="g" * 40,
        auth_mode="local",
        server_host="127.0.0.1",
        code_node_enabled=True,
        scheduler_enabled=False,
        max_concurrent_runs=2,
        max_run_tokens=10,
        max_run_seconds=10,
        max_run_steps=10,
    )
    with pytest.raises(ValueError, match="CODE_NODE_ENABLED"):
        settings.validate_public()
