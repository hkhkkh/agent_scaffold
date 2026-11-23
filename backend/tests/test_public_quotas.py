"""Anonymous visitor quotas and model-token accounting."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.runtime.ai.gateway import ModelUsage
from app.security.policy import AccessDenied, language_for, message
from app.security.quotas import (
    QuotaContext,
    QuotaExceeded,
    QuotaMiddleware,
    QuotaStore,
    get_quota_store,
    issue_visitor,
    quota_status,
    record_usage,
    reset_quota_store,
    set_visitor_cookie,
    visitor_payload,
)
from app.settings import get_settings

PUBLIC_ENV = {
    "PUBLIC_ACCESS": "true",
    "PUBLIC_DEMO": "true",
    "PUBLIC_ORIGIN": "http://testserver",
    "TRUSTED_HOSTS": "testserver",
    "AUTH_MODE": "local",
    "SERVER_HOST": "127.0.0.1",
    "GATE_COOKIE_SECRET": "g" * 40,
    "AI_REQUEST_TOKEN_LIMIT": "100",
    "VISITOR_DAILY_TOKENS": "100000",
    "IP_DAILY_TOKENS": "100000",
    "GLOBAL_DAILY_TOKENS": "1000000",
    "RATE_LIMIT_AI_PER_DAY_PER_VISITOR": "3",
    "RATE_LIMIT_AI_PER_DAY_PER_IP": "10",
    "RATE_LIMIT_RUNS_PER_DAY_PER_VISITOR": "2",
    "RATE_LIMIT_RUNS_PER_DAY_PER_IP": "2",
}


@pytest.fixture(autouse=True)
def public_quota_settings(monkeypatch, tmp_path):
    for key, value in PUBLIC_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_quota_store()
    yield
    get_settings.cache_clear()
    reset_quota_store()


def _app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AccessDenied)
    async def denied(request: Request, exc: AccessDenied):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "detail": exc.detail or message(exc.code, language_for(request)),
            },
            headers=exc.headers,
        )

    @app.post("/api/ai/fake")
    def fake_ai():
        record_usage(ModelUsage(total_tokens=150, calls=1))
        return {"ok": True}

    @app.post("/api/runs")
    def fake_run():
        return {"ok": True}

    @app.post("/api/runs/adhoc")
    def fake_adhoc_run():
        return {"ok": True}

    @app.post("/api/runs/node-test")
    def fake_node_test():
        return {"ok": True}

    @app.post("/api/runs/{run_id}/resume")
    def fake_resume(run_id: str):
        record_usage(ModelUsage(total_tokens=10, calls=1))
        return {"ok": True, "run_id": run_id}

    app.add_middleware(QuotaMiddleware)
    return app


def test_visitor_cookie_is_signed_and_does_not_accept_tampering():
    settings = get_settings()
    token, visitor = issue_visitor(settings)
    assert visitor_payload(token, settings)["vid"] == visitor

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert visitor_payload(tampered, settings) is None


def test_request_counter_is_shared_by_visitor_and_ip(tmp_path):
    store = QuotaStore(tmp_path / "quotas.db")
    store.consume_request("ai", {"v:a": 1, "ip:1.2.3.4": 5})
    with pytest.raises(QuotaExceeded):
        store.consume_request("ai", {"v:a": 1, "ip:1.2.3.4": 5})
    assert store.snapshot("v:a")["ai_requests"] == 1
    assert store.snapshot("ip:1.2.3.4")["ai_requests"] == 1


def test_token_ledger_charges_every_subject_and_global():
    store = QuotaStore(get_settings().data_dir / "quotas.db")
    store.add_tokens({"v:a": 1000, "ip:1.2.3.4": 1000}, 300, global_limit=5000)
    assert store.snapshot("v:a")["tokens"] == 300
    assert store.snapshot("ip:1.2.3.4")["tokens"] == 300
    assert store.snapshot("global")["tokens"] == 300


def test_token_ledger_refuses_after_the_limit_but_records_spend():
    store = QuotaStore(get_settings().data_dir / "quotas.db")
    with pytest.raises(QuotaExceeded):
        store.add_tokens({"v:a": 100}, 150)
    # The call still spent the tokens; the ledger must not pretend it was free.
    assert store.snapshot("v:a")["tokens"] == 150
    with pytest.raises(QuotaExceeded):
        store.check_headroom({"v:a": 100})


def test_middleware_returns_429_when_one_request_exceeds_its_token_budget():
    with TestClient(_app()) as client:
        response = client.post("/api/ai/fake")
    assert response.status_code == 429
    assert response.json()["code"] == "quota_exceeded"
    assert response.headers["Retry-After"]


@pytest.mark.parametrize(
    "path", ["/api/runs", "/api/runs/adhoc", "/api/runs/node-test"]
)
def test_middleware_enforces_the_daily_request_count(path):
    settings = get_settings()
    # One token per call stays under AI_REQUEST_TOKEN_LIMIT; the request cap
    # is what this test is about.
    assert settings.ai_request_token_limit == 100
    with TestClient(_app()) as client:
        first = client.post(path)
        second = client.post(path)
    assert first.status_code == 200
    # The default per-visitor run cap is 2, so a third request is refused. The
    # first two are both allowed and both consumed a slot.
    with TestClient(_app()) as client:
        third = client.post(path)
    assert third.status_code == 429
    assert third.json()["code"] == "quota_exceeded"


def test_resume_charges_tokens_without_spending_another_run_slot():
    with TestClient(_app()) as client:
        for _ in range(3):
            response = client.post("/api/runs/run-1/resume")
            assert response.status_code == 200

    # TestClient's peer host is not a valid IP, so client_ip() falls back to
    # "unknown"; production requests carry a real CF-Connecting-IP.
    snapshot = get_quota_store().snapshot("ip:unknown")
    assert snapshot["runs"] == 0
    assert snapshot["tokens"] == 30


def test_record_usage_without_a_request_context_is_a_noop():
    # Local mode has no QuotaContext; accounting must not raise or write.
    record_usage(ModelUsage(total_tokens=999, calls=1))


def test_quota_status_reports_remaining_allowance():
    settings = get_settings()
    # Build the status payload directly to avoid needing a browser session.
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/access/status",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
    }
    request = Request(scope)
    token, visitor = issue_visitor(settings)
    set_visitor_cookie(JSONResponse({"ok": True}), token, settings)
    status = quota_status(request, settings, visitor=visitor)
    assert status is not None
    assert status["ai_requests"]["limit"] == 3
    assert status["tokens"]["limit"] == settings.visitor_daily_tokens
    assert status["tokens"]["remaining"] == settings.visitor_daily_tokens
    assert status["per"] == "visitor"


__all__ = ["QuotaContext", "get_quota_store", "reset_quota_store"]
