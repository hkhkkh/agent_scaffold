"""The generation SSE endpoint.

Only the transport is under test here: generation itself is covered by
``test_self_test.py``. What matters is that every event reaches the client as
one `data:` frame, the last frame carries the same result the non-streaming
endpoint returns, and a failure ends the stream instead of hanging it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai_buildtime.gen_graph import GraphBuildError
from app.api import ai


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ai.router, prefix="/api")
    return TestClient(app)


def _frames(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))
    return events


def test_stream_forwards_events_then_done(monkeypatch):
    async def fake_generate(*args, **kwargs):
        on_event = kwargs["on_event"]
        await on_event({"type": "status", "phase": "building"})
        await on_event(
            {
                "type": "graph",
                "graph": {"nodes": [], "edges": [], "id": "g"},
                "changed": {"added_nodes": ["n1"]},
            }
        )
        return {"graph": {"nodes": [], "edges": [], "id": "g"}, "validation": {"ok": True}}

    monkeypatch.setattr(ai, "generate_graph", fake_generate)

    with _client() as client:
        response = client.post("/api/ai/generate-graph/stream", json={"prompt": "x"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _frames(response.text)
    assert [e["type"] for e in events] == ["status", "graph", "done"]
    assert events[-1]["result"]["validation"]["ok"] is True


def test_stream_reports_a_build_failure_as_an_error_event(monkeypatch):
    async def fake_generate(*args, **kwargs):
        raise GraphBuildError("no nodes")

    monkeypatch.setattr(ai, "generate_graph", fake_generate)

    with _client() as client:
        response = client.post("/api/ai/generate-graph/stream", json={"prompt": "x"})

    events = _frames(response.text)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "build_failed"
    assert "no nodes" in events[-1]["message"]


def test_stream_hides_generic_failure_details_in_public_mode(monkeypatch):
    """Provider text can name a base URL or echo a request; SSE must redact it too."""
    secret = "base_url=https://private.example key=sk-test-secret"

    async def fake_generate(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(ai, "generate_graph", fake_generate)
    monkeypatch.setattr(ai, "get_settings", lambda: SimpleNamespace(public_access=True))

    with _client() as client:
        response = client.post("/api/ai/generate-graph/stream", json={"prompt": "x"})

    events = _frames(response.text)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "exception"
    assert "full message is in the server log" in events[-1]["message"]
    assert "private.example" not in events[-1]["message"]
    assert "sk-test-secret" not in events[-1]["message"]
