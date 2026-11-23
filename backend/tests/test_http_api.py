"""The standard-library HTTP face: routes, auth, files, and the SSE stream.

These tests use a real socket, a real ``ThreadingHTTPServer`` and a real
``httpx`` client, because the thing under test *is* the socket-level behaviour:
status codes, headers, framing, and the fact that a request thread can run an
async agent without touching an event loop that does not exist. A fake
``TestClient`` would prove none of that.

No model is called and no network is used: the ``run_fn`` is a local async
function. The server binds port 0 and the tests read ``server.url``, so they can
run in parallel with each other or with a developer's own server.
"""

from __future__ import annotations

import base64
import contextlib
import json

import httpx
import pytest

from app.runtime.http_api import AgentHTTPServer, CHAT_PAGE, HTTPConfig
from app.runtime.session import MemorySessionStore, SessionService
from app.runtime.workspace import Workspace


# ---------------------------------------------------------------------------
# A fake agent and a running server
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Records the inputs it was given; returns a readable, deterministic output."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, inputs, emit=None):
        self.calls.append(dict(inputs))
        text = inputs.get("text")
        if isinstance(text, str) and text:
            return {"result": f"reply to: {text}"}
        return {"result": {"inputs": inputs}}


class _StreamingAgent:
    """Reports progress through ``emit`` and then answers (or fails)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def __call__(self, inputs, emit=None):
        if emit is not None:
            emit({"type": "node", "node_id": "start"})
            emit({"text": "halfway there"})
        if self.fail:
            raise RuntimeError("the model fell over")
        return {"result": "streamed answer"}


_SHARED_CLIENT: httpx.Client | None = None


def _http_client() -> httpx.Client:
    """The one httpx client the module shares.

    Constructing an ``httpx.Client`` costs a few hundred milliseconds (httpcore
    transport warm-up); one per test would dominate the suite, and every test
    only differs in which server it talks to. ``trust_env=False`` keeps the
    suite hermetic: a developer's proxy environment must not be consulted for
    127.0.0.1.
    """
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = httpx.Client(timeout=15.0, trust_env=False)
    return _SHARED_CLIENT


@pytest.fixture(scope="module", autouse=True)
def _close_the_shared_client():
    yield
    global _SHARED_CLIENT
    if _SHARED_CLIENT is not None:
        _SHARED_CLIENT.close()
        _SHARED_CLIENT = None


class _ServerClient:
    """Relative-path facade over the shared client for one running server."""

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self._client = client
        self._base_url = base_url

    def _url(self, path: str) -> str:
        return self._base_url + path

    def get(self, path, **kwargs):
        return self._client.get(self._url(path), **kwargs)

    def post(self, path, **kwargs):
        return self._client.post(self._url(path), **kwargs)

    def delete(self, path, **kwargs):
        return self._client.delete(self._url(path), **kwargs)

    def put(self, path, **kwargs):
        return self._client.put(self._url(path), **kwargs)

    def request(self, method, path, **kwargs):
        return self._client.request(method, self._url(path), **kwargs)

    @contextlib.contextmanager
    def stream(self, method, path, **kwargs):
        with self._client.stream(method, self._url(path), **kwargs) as response:
            yield response


@contextlib.contextmanager
def _client(
    tmp_path,
    run_fn,
    *,
    token: str = "",
    sessions: bool = True,
    max_body_bytes: int | None = None,
    workspace_factory=None,
    agent_name: str = "test-agent",
):
    """Start an AgentHTTPServer on a free port and yield a base-url client."""
    config = HTTPConfig(
        host="127.0.0.1", port=0, token=token, agent_name=agent_name
    )
    if max_body_bytes is not None:
        config.max_body_bytes = max_body_bytes
    service = SessionService(MemorySessionStore(), run_fn) if sessions else None
    factory = workspace_factory or (lambda: Workspace(tmp_path / "workspace"))
    server = AgentHTTPServer(
        run_fn=run_fn,
        session_service=service,
        workspace_factory=factory,
        config=config,
    )
    thread = server.start()
    try:
        yield _ServerClient(_http_client(), server.url)
    finally:
        server.close()
        thread.join(timeout=5.0)


def _sse_events(body: str) -> list[dict]:
    """Every ``data:`` frame in an SSE body, in order."""
    events: list[dict] = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


@pytest.fixture
def fake_agent() -> _FakeAgent:
    return _FakeAgent()


# ---------------------------------------------------------------------------
# The surfaces that need no token
# ---------------------------------------------------------------------------


class TestTheBasics:
    def test_healthz_is_healthy_and_plain_json(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.headers["content-type"].startswith("application/json")

    def test_the_root_serves_the_built_in_chat_page(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        page = response.text
        assert page == CHAT_PAGE
        assert page.startswith("<!DOCTYPE html>")
        # The three things the task asks the page to contain, as behaviour:
        assert "新建会话" in page
        assert "FileReader" in page  # the upload button really base64-encodes
        assert "/v1/sessions" in page and "/v1/files" in page
        assert "fetch(" in page

    def test_the_chat_page_has_no_external_dependency(self, tmp_path, fake_agent):
        """No CDN, no font download, no build step - it must work offline."""
        page = CHAT_PAGE
        assert "https://" not in page
        assert "http://" not in page
        assert "<link" not in page
        assert "<script src" not in page

    def test_info_describes_the_deployment(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, agent_name="demo") as client:
            info = client.get("/api/info")

        assert info.status_code == 200
        payload = info.json()
        assert payload["agent"] == "demo"
        assert payload["sessions"] is True
        assert payload["files"] is True

    def test_info_says_sessions_are_off_when_they_are(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, sessions=False) as client:
            assert client.get("/api/info").json()["sessions"] is False

    def test_an_unknown_path_is_a_json_404(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.get("/nope")

        assert response.status_code == 404
        assert response.json() == {"ok": False, "error": "not found"}

    def test_an_unsupported_method_is_405(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.put("/v1/sessions")

        assert response.status_code == 405
        assert response.json()["ok"] is False

    def test_sessions_routes_report_503_when_no_service_was_configured(
        self, tmp_path, fake_agent
    ):
        with _client(tmp_path, fake_agent, sessions=False) as client:
            listed = client.get("/v1/sessions")
            created = client.post("/v1/sessions", json={"title": "x"})

        assert listed.status_code == 503
        assert created.status_code == 503
        assert listed.json()["ok"] is False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def test_create_list_and_fetch_a_session(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            created = client.post("/v1/sessions", json={"title": "预算评审"})
            listed = client.get("/v1/sessions")
            fetched = client.get(f"/v1/sessions/{created.json()['session']['id']}")

        assert created.status_code == 201
        session = created.json()["session"]
        assert session["title"] == "预算评审"
        assert session["message_count"] == 0
        assert [item["id"] for item in listed.json()["sessions"]] == [session["id"]]
        assert fetched.status_code == 200
        assert fetched.json()["session"]["id"] == session["id"]
        assert fetched.json()["messages"] == []

    def test_a_turn_returns_both_messages_and_the_run_output(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            turn = client.post(
                f"/v1/sessions/{session_id}/messages", json={"text": "hello"}
            )
            detail = client.get(f"/v1/sessions/{session_id}")

        assert turn.status_code == 200
        payload = turn.json()
        assert payload["ok"] is True
        assert payload["user"]["content"] == "hello"
        assert payload["assistant"]["content"] == "reply to: hello"
        assert payload["output"] == {"result": "reply to: hello"}

        messages = detail.json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert [message["content"] for message in messages] == [
            "hello",
            "reply to: hello",
        ]
        assert detail.json()["session"]["message_count"] == 2

    def test_the_second_turn_sends_the_first_one_as_history(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            client.post(f"/v1/sessions/{session_id}/messages", json={"text": "first"})
            client.post(f"/v1/sessions/{session_id}/messages", json={"text": "second"})

        first, second = fake_agent.calls
        assert first["text"] == "first"
        assert first["history"] == ""
        assert second["history"] == "user: first\nassistant: reply to: first\n"

    def test_two_sessions_do_not_share_messages(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            first = client.post("/v1/sessions", json={}).json()["session"]["id"]
            second = client.post("/v1/sessions", json={}).json()["session"]["id"]
            client.post(f"/v1/sessions/{first}/messages", json={"text": "for one"})
            other = client.get(f"/v1/sessions/{second}")

        assert other.json()["messages"] == []

    def test_an_unknown_session_is_404(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            fetched = client.get("/v1/sessions/nope")
            turn = client.post("/v1/sessions/nope/messages", json={"text": "hi"})

        assert fetched.status_code == 404
        assert turn.status_code == 404
        assert fetched.json()["ok"] is False

    def test_delete_a_session_then_it_is_gone(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            deleted = client.delete(f"/v1/sessions/{session_id}")
            fetched = client.get(f"/v1/sessions/{session_id}")

        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True}
        assert fetched.status_code == 404

    def test_deleting_an_unknown_session_is_404(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            assert client.delete("/v1/sessions/nope").status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"text": ""},
            {"text": "   "},
            {"text": 42},
            {"text": "ok", "attachments": "nope"},
            {"text": "ok", "attachments": [1, 2]},
        ],
    )
    def test_a_bad_message_body_is_400(self, tmp_path, fake_agent, body):
        with _client(tmp_path, fake_agent) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            response = client.post(f"/v1/sessions/{session_id}/messages", json=body)

        assert response.status_code == 400, response.text
        assert response.json()["ok"] is False

    def test_malformed_json_is_400_not_500(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.post(
                "/v1/invoke",
                content=b"{ this is not json",
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 400
        assert response.json() == {"ok": False, "error": "request body is not valid JSON"}

    def test_a_non_object_json_body_is_400(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.post("/v1/invoke", content=b"[1, 2, 3]")

        assert response.status_code == 400
        assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# One-shot invocation (no conversation)
# ---------------------------------------------------------------------------


class TestInvoke:
    def test_invoke_runs_the_agent_once(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.post("/v1/invoke", json={"input": {"query": "hi"}})

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "output": {"result": {"inputs": {"query": "hi"}}},
        }
        assert fake_agent.calls == [{"query": "hi"}]

    def test_invoke_without_input_runs_with_an_empty_dict(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.post("/v1/invoke", json={})

        assert response.status_code == 200
        assert fake_agent.calls == [{}]

    def test_a_non_object_input_is_400(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.post("/v1/invoke", json={"input": [1, 2]})

        assert response.status_code == 400
        assert response.json()["ok"] is False

    def test_a_crashing_run_is_a_json_500_without_a_traceback(self, tmp_path):
        async def boom(inputs, emit=None):
            raise RuntimeError("the database is on fire")

        with _client(tmp_path, boom) as client:
            response = client.post("/v1/invoke", json={"input": {}})

        assert response.status_code == 500
        payload = response.json()
        assert payload["ok"] is False
        assert "the database is on fire" in payload["error"]
        assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# Files: upload, list, download, delete - inside the workspace
# ---------------------------------------------------------------------------


def _upload(client, name: str, data: bytes):
    return client.post(
        "/v1/files",
        json={"name": name, "content_base64": base64.b64encode(data).decode("ascii")},
    )


class TestFiles:
    def test_upload_list_download_delete_roundtrip(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            uploaded = _upload(client, "rates.csv", "a,b\n1,2\n".encode("utf-8"))
            listed = client.get("/v1/files")
            downloaded = client.get("/v1/files/rates.csv")
            deleted = client.delete("/v1/files/rates.csv")
            after = client.get("/v1/files")

        assert uploaded.status_code == 201
        assert uploaded.json()["ok"] is True
        assert uploaded.json()["file"] == {"name": "rates.csv", "size": 8}

        files = listed.json()["files"]
        assert [item["name"] for item in files] == ["rates.csv"]
        assert files[0]["size"] == 8
        assert isinstance(files[0]["modified"], (int, float))

        assert downloaded.status_code == 200
        assert downloaded.content == b"a,b\n1,2\n"
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["content-type"] == "application/octet-stream"

        assert deleted.json() == {"ok": True}
        assert after.json()["files"] == []

    def test_a_chinese_file_name_survives_upload_and_download(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            uploaded = _upload(client, "季度报告.txt", "完成".encode("utf-8"))
            downloaded = client.get("/v1/files/季度报告.txt")
            listed = client.get("/v1/files")

        assert uploaded.status_code == 201
        assert downloaded.status_code == 200
        assert downloaded.content == "完成".encode("utf-8")
        assert "filename*=UTF-8''" in downloaded.headers["content-disposition"]
        assert [item["name"] for item in listed.json()["files"]] == ["季度报告.txt"]

    def test_a_subdirectory_file_is_allowed(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            uploaded = _upload(client, "reports/q1.txt", b"q1")
            downloaded = client.get("/v1/files/reports/q1.txt")

        assert uploaded.status_code == 201
        assert downloaded.content == b"q1"

    def test_downloading_a_missing_file_is_404(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.get("/v1/files/never-there.txt")

        assert response.status_code == 404
        assert response.json()["ok"] is False

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/files/..%2Fsecret.txt",
            "/v1/files/%2E%2E%2Fsecret.txt",
            "/v1/files/sub%2F..%2F..%2Fsecret.txt",
            "/v1/files/CON",
        ],
    )
    def test_traversal_and_reserved_names_are_400(self, tmp_path, fake_agent, path):
        with _client(tmp_path, fake_agent) as client:
            response = client.get(path)

        assert response.status_code == 400, (path, response.status_code, response.text)
        assert response.json()["ok"] is False

    def test_deleting_a_traversal_path_is_400(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.delete("/v1/files/..%2Fsecret.txt")

        assert response.status_code == 400

    def test_an_absolute_name_is_refused(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = _upload(client, "C:\\Windows\\win.ini", b"x")

        assert response.status_code == 400
        assert response.json()["ok"] is False

    def test_invalid_base64_is_400(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.post(
                "/v1/files",
                json={"name": "bad.bin", "content_base64": "not base64 at all!!"},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "'content_base64' is not valid base64"

    def test_a_body_over_the_configured_limit_is_413(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, max_body_bytes=128) as client:
            response = _upload(client, "big.bin", b"x" * 4096)

        assert response.status_code == 413
        assert response.json()["ok"] is False

    def test_a_file_over_the_workspace_limit_is_413(self, tmp_path, fake_agent):
        factory = lambda: Workspace(tmp_path / "workspace", max_write_bytes=4)  # noqa: E731
        with _client(tmp_path, fake_agent, workspace_factory=factory) as client:
            response = _upload(client, "too-big.bin", b"12345")

        assert response.status_code == 413
        assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


class TestTokenAuth:
    def test_healthz_and_the_page_never_need_the_token(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, token="s3cret") as client:
            health = client.get("/healthz")
            page = client.get("/")

        assert health.status_code == 200
        assert page.status_code == 200

    def test_api_routes_require_the_bearer_token(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, token="s3cret") as client:
            anonymous = client.get("/v1/sessions")
            wrong = client.get(
                "/v1/sessions", headers={"Authorization": "Bearer wrong"}
            )
            authorized = client.get(
                "/v1/sessions", headers={"Authorization": "Bearer s3cret"}
            )
            info_anonymous = client.get("/api/info")
            info_authorized = client.get(
                "/api/info", headers={"Authorization": "Bearer s3cret"}
            )

        assert anonymous.status_code == 401
        assert wrong.status_code == 401
        assert authorized.status_code == 200
        assert info_anonymous.status_code == 401
        assert info_authorized.status_code == 200
        assert anonymous.json()["ok"] is False
        assert "Traceback" not in anonymous.text

    def test_a_401_does_not_leak_whether_the_session_exists(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, token="s3cret") as client:
            response = client.get("/v1/sessions/anything")

        assert response.status_code == 401

    def test_preflight_is_allowed_before_authentication(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent, token="s3cret") as client:
            response = client.request("OPTIONS", "/v1/sessions")

        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCors:
    def test_the_v1_preflight_answers_204_with_the_contract_headers(
        self, tmp_path, fake_agent
    ):
        with _client(tmp_path, fake_agent) as client:
            response = client.request(
                "OPTIONS",
                "/v1/sessions",
                headers={
                    "Origin": "http://example.test",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization",
                },
            )

        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == "*"
        assert "POST" in response.headers["access-control-allow-methods"]
        assert "Authorization" in response.headers["access-control-allow-headers"]
        assert response.headers["access-control-max-age"] == "86400"

    def test_actual_v1_responses_carry_the_cors_origin(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.get("/v1/sessions")

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"

    def test_non_v1_responses_do_not_carry_cors_headers(self, tmp_path, fake_agent):
        with _client(tmp_path, fake_agent) as client:
            response = client.get("/healthz")

        assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Server-sent events: progress first, one done at the end
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_the_stream_reports_progress_then_done(self, tmp_path):
        with _client(tmp_path, _StreamingAgent()) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            response = client.post(
                f"/v1/sessions/{session_id}/messages?stream=1",
                json={"text": "stream please"},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(response.text)
        assert [event["type"] for event in events] == ["node", "progress", "done"]
        assert events[0]["node_id"] == "start"
        # An emit payload without a type is wrapped, so the frame is always tagged.
        assert events[1]["data"] == {"text": "halfway there"}
        assert events[2]["assistant"]["content"] == "streamed answer"
        assert events[2]["output"] == {"result": "streamed answer"}

    def test_the_stream_records_the_turn_in_the_transcript(self, tmp_path):
        with _client(tmp_path, _StreamingAgent()) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            client.post(
                f"/v1/sessions/{session_id}/messages?stream=1",
                json={"text": "stream please"},
            )
            detail = client.get(f"/v1/sessions/{session_id}")

        assert [message["role"] for message in detail.json()["messages"]] == [
            "user",
            "assistant",
        ]
        assert detail.json()["messages"][1]["content"] == "streamed answer"

    def test_a_failed_stream_ends_with_an_error_frame(self, tmp_path):
        with _client(tmp_path, _StreamingAgent(fail=True)) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            response = client.post(
                f"/v1/sessions/{session_id}/messages?stream=1",
                json={"text": "boom"},
            )
            detail = client.get(f"/v1/sessions/{session_id}")

        assert response.status_code == 200
        events = _sse_events(response.text)
        assert events[-1]["type"] == "error"
        assert "the model fell over" in events[-1]["error"]
        # The failed turn is still in the transcript, marked as failed.
        messages = detail.json()["messages"]
        assert messages[-1]["role"] == "assistant"
        assert "运行失败" in messages[-1]["content"]

    def test_streaming_an_unknown_session_is_a_plain_404(self, tmp_path):
        with _client(tmp_path, _StreamingAgent()) as client:
            response = client.post(
                "/v1/sessions/nope/messages?stream=1", json={"text": "hi"}
            )

        assert response.status_code == 404
        assert response.json()["ok"] is False

    def test_streaming_an_invalid_body_is_a_plain_400(self, tmp_path):
        with _client(tmp_path, _StreamingAgent()) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            response = client.post(
                f"/v1/sessions/{session_id}/messages?stream=1", json={"text": ""}
            )

        assert response.status_code == 400
        assert response.json()["ok"] is False

    def test_stream_false_is_the_json_path(self, tmp_path):
        with _client(tmp_path, _StreamingAgent()) as client:
            session_id = client.post("/v1/sessions", json={}).json()["session"]["id"]
            response = client.post(
                f"/v1/sessions/{session_id}/messages?stream=0",
                json={"text": "not streamed"},
            )

        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["assistant"]["content"] == "streamed answer"


# ---------------------------------------------------------------------------
# The server object itself
# ---------------------------------------------------------------------------


class TestTheServerObject:
    def test_url_reports_the_real_bound_port(self, tmp_path, fake_agent):
        server = AgentHTTPServer(
            run_fn=fake_agent,
            session_service=SessionService(MemorySessionStore(), fake_agent),
            workspace_factory=lambda: Workspace(tmp_path / "workspace"),
            config=HTTPConfig(host="127.0.0.1", port=0),
        )
        try:
            assert server.url.startswith("http://127.0.0.1:")
            assert server.port > 0
            assert server.url.endswith(f":{server.port}")
        finally:
            server.server_close()

    def test_the_context_manager_starts_and_stops(self, tmp_path, fake_agent):
        server = AgentHTTPServer(
            run_fn=fake_agent,
            config=HTTPConfig(host="127.0.0.1", port=0),
        )
        with server:
            client = _ServerClient(_http_client(), server.url)
            assert client.get("/healthz").status_code == 200

    def test_the_chat_page_can_be_switched_off(self, tmp_path, fake_agent):
        server = AgentHTTPServer(
            run_fn=fake_agent,
            config=HTTPConfig(host="127.0.0.1", port=0, chat_page=False),
        )
        thread = server.start()
        try:
            client = _ServerClient(_http_client(), server.url)
            response = client.get("/")
        finally:
            server.close()
            thread.join(timeout=5.0)

        assert response.status_code == 200
        assert response.json()["chat_page"] is False
