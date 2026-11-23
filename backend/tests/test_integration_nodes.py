"""Business connector nodes: auth, paging, read-only SQL, mail, PDF text.

These are the nodes that touch the outside world, so the tests care about the
boundaries as much as the happy path: the egress policy is never bypassed, SQL
cannot write, a document path cannot escape DATA_DIR, and the public demo
refuses the private connectors.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.graph.schema import NodeSpec
from app.nodes.base import NodeContext
from app.nodes.integrations import (
    ApiCallNode as _ApiCallNode,
    DocumentExtractNode as _DocumentExtractNode,
    EmailSendNode as _EmailSendNode,
    FhirQueryNode as _FhirQueryNode,
    SqlQueryNode as _SqlQueryNode,
    _check_read_only,
    _parse_addresses,
    _run_sql,
)
from app.settings import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _spec(node_type: str) -> NodeSpec:
    return NodeSpec(id="probe", type=node_type, label="probe")


def ApiCallNode() -> _ApiCallNode:  # noqa: N802 - matches the node class name
    return _ApiCallNode(_spec("api_call"))


def FhirQueryNode() -> _FhirQueryNode:  # noqa: N802
    return _FhirQueryNode(_spec("fhir_query"))


def SqlQueryNode() -> _SqlQueryNode:  # noqa: N802
    return _SqlQueryNode(_spec("sql_query"))


def EmailSendNode() -> _EmailSendNode:  # noqa: N802
    return _EmailSendNode(_spec("email_send"))


def DocumentExtractNode() -> _DocumentExtractNode:  # noqa: N802
    return _DocumentExtractNode(_spec("document_extract"))


def _ctx(node_type: str, config: dict[str, Any], settings: Settings | None = None) -> NodeContext:
    async def emit(_payload: dict[str, Any]) -> None:
        return None

    async def log(_text: str, _level: str = "info") -> None:
        return None

    node = NodeSpec(id="probe", type=node_type, label="probe", config=config)
    return NodeContext(
        node=node,
        config=config,
        inputs={"in": None},
        state={"vars": {"start": {"out": {"id": "42"}}}},
        settings=settings or _settings(),
        emit=emit,
        log=log,
    )


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------


async def test_api_call_renders_templates_and_applies_bearer_auth(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake(url: str, **kwargs: Any) -> dict[str, Any]:
        seen["url"] = url
        seen.update(kwargs)
        return {"ok": True, "status": 200, "body": {"result": "ok"}}

    monkeypatch.setattr("app.nodes.integrations._http_request", fake)
    monkeypatch.setenv("PROBE_TOKEN", "s3cret")

    node = ApiCallNode()
    ctx = _ctx(
        "api_call",
        {
            "url": "https://api.example/patients/{{ vars.start.out.id }}",
            "method": "GET",
            "auth_kind": "bearer",
            "bearer_token": "env:PROBE_TOKEN",
            "params": {"q": "{{ vars.start.out.id }}"},
            "headers": {"X-Trace": "{{ vars.start.out.id }}"},
        },
    )

    result = await node.run(ctx)

    assert seen["url"] == "https://api.example/patients/42"
    assert seen["params"] == {"q": "42"}
    assert seen["headers"]["X-Trace"] == "42"
    assert seen["headers"]["Authorization"] == "Bearer s3cret"
    assert result["out"] == {"result": "ok"}
    assert result["status"] == 200


async def test_api_call_oauth2_fetches_a_token_before_the_request(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs.get("method", "GET")))
        if url.endswith("/token"):
            assert kwargs["form"]["grant_type"] == "client_credentials"
            return {"ok": True, "status": 200, "body": {"access_token": "tok-1"}}
        assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
        return {"ok": True, "status": 200, "body": {"ok": True}}

    monkeypatch.setattr("app.nodes.integrations._http_request", fake)

    node = ApiCallNode()
    ctx = _ctx(
        "api_call",
        {
            "url": "https://fhir.example/r4/Patient",
            "auth_kind": "oauth2",
            "oauth_token_url": "https://auth.example/token",
            "oauth_client_id": "client",
            "oauth_client_secret": "env:PROBE_OAUTH_SECRET",
            "oauth_scope": "patient/*.read",
        },
    )
    monkeypatch.setenv("PROBE_OAUTH_SECRET", "secret")

    await node.run(ctx)

    assert calls == [
        ("https://auth.example/token", "POST"),
        ("https://fhir.example/r4/Patient", "GET"),
    ]


async def test_api_call_can_capture_an_error_instead_of_failing(monkeypatch):
    async def fake(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"error": "egress_denied", "detail": "host not permitted"}

    monkeypatch.setattr("app.nodes.integrations._http_request", fake)
    node = ApiCallNode()
    ctx = _ctx(
        "api_call",
        {"url": "https://blocked.example", "fail_on_error": False},
    )

    result = await node.run(ctx)

    assert result["out"]["error"] == "egress_denied"
    assert result["status"] == 0


# ---------------------------------------------------------------------------
# FHIR
# ---------------------------------------------------------------------------


async def test_fhir_follows_bundle_next_links_until_max_pages(monkeypatch):
    urls: list[str] = []

    async def fake(url: str, **kwargs: Any) -> dict[str, Any]:
        urls.append(url)
        if len(urls) == 1:
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "resourceType": "Bundle",
                    "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
                    "link": [{"relation": "next", "url": "https://fhir.example/next"}],
                },
            }
        return {
            "ok": True,
            "status": 200,
            "body": {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Observation", "id": "o2"}}],
            },
        }

    monkeypatch.setattr("app.nodes.integrations._http_request", fake)
    node = FhirQueryNode()
    ctx = _ctx(
        "fhir_query",
        {
            "base_url": "https://fhir.example/r4/",
            "resource": "Observation",
            "search": {"patient": "p1"},
            "page_size": 2,
            "max_pages": 5,
        },
    )

    result = await node.run(ctx)

    assert [item["id"] for item in result["out"]] == ["o1", "o2"]
    assert result["count"] == 2
    assert urls == ["https://fhir.example/r4/Observation", "https://fhir.example/next"]


async def test_fhir_stops_at_max_pages(monkeypatch):
    async def fake(url: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "status": 200,
            "body": {
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": f"{url}?page=next"}],
            },
        }

    monkeypatch.setattr("app.nodes.integrations._http_request", fake)
    node = FhirQueryNode()
    ctx = _ctx(
        "fhir_query",
        {"base_url": "https://fhir.example/r4", "resource": "Patient", "max_pages": 2},
    )

    result = await node.run(ctx)

    assert result["count"] == 0


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def test_sql_is_read_only():
    assert _check_read_only("SELECT * FROM patients") == "SELECT * FROM patients"
    assert _check_read_only("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")

    for statement in (
        "DELETE FROM patients",
        "UPDATE patients SET name = 'x'",
        "DROP TABLE patients",
        "SELECT 1; DROP TABLE patients",
        "SELECT * INTO copy FROM patients",
    ):
        with pytest.raises(ValueError):
            _check_read_only(statement)


def test_sqlite_query_returns_rows(tmp_path):
    path = tmp_path / "biz.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
    conn.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10.5), (2, 20.0)])
    conn.commit()
    conn.close()

    rows = _run_sql(
        {
            "driver": "sqlite",
            "sqlite_path": str(path),
            "query": "SELECT id, amount FROM orders ORDER BY id",
            "params": [],
            "max_rows": 10,
        }
    )

    assert rows == [{"id": 1, "amount": 10.5}, {"id": 2, "amount": 20.0}]


async def test_sql_node_reports_a_write_attempt_as_an_error():
    node = SqlQueryNode()
    ctx = _ctx(
        "sql_query",
        {
            "driver": "sqlite",
            "sqlite_path": "unused.db",
            "query": "DELETE FROM orders",
            "fail_on_error": False,
        },
    )

    result = await node.run(ctx)

    assert "read-only" in result["out"]["error"]
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def test_email_addresses_are_split_on_commas_and_semicolons():
    assert _parse_addresses("a@x.com; b@x.com, c@x.com") == [
        "a@x.com",
        "b@x.com",
        "c@x.com",
    ]


async def test_email_node_renders_recipients_and_body(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_send(config: dict[str, Any]) -> int:
        captured.update(config)
        return 2

    monkeypatch.setattr("app.nodes.integrations._send_mail", fake_send)
    node = EmailSendNode()
    ctx = _ctx(
        "email_send",
        {
            "smtp_host": "mail.internal",
            "from_addr": "bot@hospital.example",
            "to": "doctor@x.com; nurse@x.com",
            "subject": "Report {{ vars.start.out.id }}",
            "body": "Case {{ vars.start.out.id }} is ready.",
        },
    )

    result = await node.run(ctx)

    assert captured["subject"] == "Report 42"
    assert captured["body"] == "Case 42 is ready."
    assert result["out"] == {"sent": True, "recipients": 2}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _text_pdf(text: str) -> bytes:
    """A minimal one-page PDF with Helvetica text; enough for pypdf."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 44 >>\nstream\nBT /F1 18 Tf 20 100 Td (Hello hospital) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


async def test_document_extract_reads_a_file_under_data_dir(tmp_path):
    settings = _settings(data_dir=str(tmp_path))
    (tmp_path / "letter.pdf").write_bytes(_text_pdf("Hello hospital"))

    node = DocumentExtractNode()
    ctx = _ctx(
        "document_extract",
        {"source": "file", "file_path": "letter.pdf"},
        settings=settings,
    )

    result = await node.run(ctx)

    assert "Hello hospital" in result["out"]
    assert result["pages"] == 1


async def test_document_extract_refuses_a_path_outside_data_dir(tmp_path):
    settings = _settings(data_dir=str(tmp_path / "data"))
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4")

    node = DocumentExtractNode()
    ctx = _ctx(
        "document_extract",
        {"source": "file", "file_path": str(outside), "fail_on_error": False},
        settings=settings,
    )

    result = await node.run(ctx)

    assert "DATA_DIR" in result["out"]


async def test_private_connectors_refuse_the_public_demo():
    settings = _settings(public_demo=True)
    node = SqlQueryNode()
    ctx = _ctx(
        "sql_query",
        {"driver": "sqlite", "sqlite_path": "x.db", "query": "SELECT 1"},
        settings=settings,
    )

    with pytest.raises(ValueError, match="public demo"):
        await node.run(ctx)


def test_public_demo_policy_refuses_a_private_connector_graph():
    from app.graph.schema import WorkflowGraph
    from app.security.policy import AccessDenied, enforce_public_graph_policy

    graph = WorkflowGraph.model_validate(
        {
            "id": "g",
            "name": "private",
            "nodes": [
                {"id": "s", "type": "start", "label": "Start"},
                {"id": "q", "type": "sql_query", "label": "Query"},
            ],
            "edges": [],
        }
    )

    with pytest.raises(AccessDenied):
        enforce_public_graph_policy(graph, _settings(public_demo=True))
