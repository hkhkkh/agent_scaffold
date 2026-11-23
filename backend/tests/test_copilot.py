"""Scope and layout behaviour of the editing copilot.

The model-driven part is exercised by the real end-to-end generation tests;
these pin the two guarantees that must hold even when the model misbehaves:
a confirm-mode edit cannot write outside the approved nodes, and an edit cannot
re-flow the user's canvas.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai_buildtime.copilot import (
    ScopedDraft,
    _allowed_nodes,
    _clean_history,
    _regressed,
)
from app.ai_buildtime.prompts import build_copilot_insight_messages
from app.api import ai
from app.graph.schema import Position, WorkflowGraph


def _graph() -> WorkflowGraph:
    return WorkflowGraph.model_validate(
        {
            "id": "g",
            "name": "test",
            "nodes": [
                {
                    "id": "s",
                    "type": "start",
                    "label": "Start",
                    "position": {"x": 10, "y": 20},
                },
                {
                    "id": "p",
                    "type": "prompt",
                    "label": "Prompt",
                    "position": {"x": 300, "y": 40},
                    "config": {"template": "hello"},
                },
                {
                    "id": "e",
                    "type": "end",
                    "label": "End",
                    "position": {"x": 600, "y": 60},
                },
            ],
            "edges": [
                {
                    "id": "s-p",
                    "source": "s",
                    "target": "p",
                    "sourceHandle": "out",
                    "targetHandle": "in",
                },
                {
                    "id": "p-e",
                    "source": "p",
                    "target": "e",
                    "sourceHandle": "out",
                    "targetHandle": "in",
                },
            ],
        }
    )


def test_scoped_draft_allows_selected_nodes_and_refuses_others():
    draft = ScopedDraft(_graph(), allowed={"p"})

    assert draft.update_node("p", config={"template": "new"}).startswith("Updated")
    assert draft.update_node("s", label="nope").startswith("ERROR")
    assert draft.update_node("e", label="nope").startswith("ERROR")
    assert draft.delete_node("s").startswith("ERROR")


def test_scoped_draft_allows_boundary_connections_but_not_both_outside():
    draft = ScopedDraft(_graph(), allowed={"p"})

    assert "ERROR" not in draft.connect("p", "e", label="ok")
    assert "ERROR" not in draft.connect("s", "p", label="ok")
    assert draft.connect("s", "e", label="nope").startswith("ERROR")


def test_a_new_node_is_writable_even_when_it_is_outside_the_selection():
    draft = ScopedDraft(_graph(), allowed={"p"})
    result = draft.add_node("prompt", "New")
    assert result.startswith("Added")

    new_id = next(node.id for node in draft.nodes if node.id not in {"s", "p", "e"})
    assert draft.update_node(new_id, description="changed").startswith("Updated")


def test_disconnect_removes_only_the_named_connection():
    draft = ScopedDraft(_graph(), allowed={"p"})

    result = draft.disconnect("p", "e")
    assert result.startswith("Removed 1")
    assert [edge.id for edge in draft.edges] == ["s-p"]
    assert draft.disconnect("p", "e").startswith("ERROR")


def test_an_edit_preserves_existing_positions():
    original = _graph()
    draft = ScopedDraft(original)
    draft.update_node("p", config={"template": "changed"})

    edited = draft.to_graph(preserve_positions=True)
    for node_id, (x, y) in {"s": (10, 20), "p": (300, 40), "e": (600, 60)}.items():
        node = edited.node(node_id)
        assert node is not None
        assert node.position == Position(x=x, y=y)


def test_a_new_node_is_placed_near_its_neighbours():
    draft = ScopedDraft(_graph())
    draft.add_node("prompt", "New")
    new_id = next(node.id for node in draft.nodes if node.id not in {"s", "p", "e"})
    draft.connect("p", new_id, label="after prompt")
    draft.connect(new_id, "e", label="before end")

    edited = draft.to_graph(preserve_positions=True)
    placed = edited.node(new_id)
    assert placed is not None
    # Somewhere between the prompt and the end, not at the origin and not
    # stacked on top of an existing card.
    assert placed.position.x != 0
    assert 300 <= placed.position.x <= 600


def test_repeated_snapshots_do_not_move_a_node():
    """Placement is not idempotent, so a node must be placed exactly once.

    Every mutating tool call publishes a snapshot, and validation and self-test
    call `to_graph` again. Re-running the neighbour-average placement on each
    call compounded its stagger offset: a live build drifted tens of thousands
    of pixels off-screen while the nodes were all still in the graph.
    """
    draft = ScopedDraft(_graph())
    draft.add_node("prompt", "New")
    new_id = next(node.id for node in draft.nodes if node.id not in {"s", "p", "e"})
    draft.connect("p", new_id, label="after prompt")

    first = {
        node.id: (node.position.x, node.position.y)
        for node in draft.to_graph(preserve_positions=True).nodes
    }
    for _ in range(50):
        again = draft.to_graph(preserve_positions=True)
        assert {
            node.id: (node.position.x, node.position.y) for node in again.nodes
        } == first


def test_building_from_an_empty_canvas_keeps_the_graph_on_screen():
    draft = ScopedDraft(None)
    previous: str | None = None
    for index in range(12):
        before = {node.id for node in draft.nodes}
        draft.add_node("prompt", f"Step {index}")
        new_id = next(iter({node.id for node in draft.nodes} - before))
        if previous:
            draft.connect(previous, new_id, label="next")
        previous = new_id
        draft.to_graph(preserve_positions=True)

    graph = draft.to_graph(preserve_positions=True)
    # A fresh build used to end up at y≈22000 because every snapshot re-anchored
    # every node. Bounded coordinates are the observable guarantee.
    assert max(abs(node.position.y) for node in graph.nodes) < 1000
    assert max(abs(node.position.x) for node in graph.nodes) < 5000
    positions = {(node.position.x, node.position.y) for node in graph.nodes}
    assert len(positions) == len(graph.nodes)


def test_confirm_mode_without_a_selection_uses_the_plan_focus():
    allowed = _allowed_nodes(_graph(), [], {"focus_nodes": ["p"]}, "confirm")
    assert allowed == {"p"}


def test_a_change_named_in_the_approved_plan_is_writable():
    plan = {"changes": [{"action": "update", "target": "e", "why": "output"}]}
    allowed = _allowed_nodes(_graph(), ["p"], plan, "confirm")
    assert allowed == {"p", "e"}


def test_confirm_mode_with_no_region_can_only_add_new_nodes():
    assert _allowed_nodes(_graph(), [], {"changes": []}, "confirm") == set()


def test_auto_mode_has_no_write_scope():
    assert _allowed_nodes(_graph(), ["p"], {"focus_nodes": []}, "auto") is None


def test_history_is_bounded_and_drops_invalid_roles():
    history = [
        {"role": "system", "content": "ignore me"},
        *[
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn {index}"}
            for index in range(12)
        ],
    ]
    cleaned = _clean_history(history)
    assert len(cleaned) == 8
    assert cleaned[0]["content"] == "turn 4"
    assert all(item["role"] in {"user", "assistant"} for item in cleaned)


def test_history_clips_an_overlong_message():
    cleaned = _clean_history([{"role": "user", "content": "x" * 5000}])
    assert len(cleaned[0]["content"]) == 2000


def test_earlier_turns_are_in_the_prompt_before_the_current_request():
    messages = build_copilot_insight_messages(
        _graph(),
        "再短一点，每条不超过五个字",
        history=[
            {"role": "user", "content": "把输出改成三条要点"},
            {"role": "assistant", "content": "好的，已经改成三条。"},
        ],
        lang="zh",
    )
    prompt = messages[1]["content"]
    assert "Earlier in this conversation" in prompt
    assert prompt.index("把输出改成三条要点") < prompt.index("再短一点")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


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


def test_copilot_stream_forwards_stage_events_and_done(monkeypatch):
    async def fake_copilot(*args, **kwargs):
        on_event = kwargs["on_event"]
        await on_event({"type": "stage", "stage": "insight", "status": "running"})
        await on_event({"type": "brief", "brief": {"goal": "do the thing"}})
        return {"graph": {"nodes": [], "edges": [], "id": "g"}, "needs_approval": True}

    monkeypatch.setattr(ai, "run_copilot", fake_copilot)

    with _client() as client:
        response = client.post(
            "/api/ai/copilot/stream",
            json={
                "message": "change the output",
                "graph": {"nodes": [], "edges": [], "id": "g"},
                "mode": "confirm",
            },
        )

    assert response.status_code == 200
    events = _frames(response.text)
    assert [event["type"] for event in events] == ["stage", "brief", "done"]
    assert events[-1]["result"]["needs_approval"] is True


def test_copilot_stream_reports_a_failure_as_an_error_event(monkeypatch):
    async def fake_copilot(*args, **kwargs):
        from app.ai_buildtime.copilot import CopilotError

        raise CopilotError("plan failed")

    monkeypatch.setattr(ai, "run_copilot", fake_copilot)

    with _client() as client:
        response = client.post(
            "/api/ai/copilot/stream",
            json={"message": "x", "graph": {"nodes": [], "edges": [], "id": "g"}},
        )

    events = _frames(response.text)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "build_failed"


def test_copilot_stream_hides_generic_failure_details_in_public_mode(monkeypatch):
    """The copilot stream must redact provider text just like the JSON endpoint."""
    secret = "base_url=https://private.example key=sk-test-secret"

    async def fake_copilot(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(ai, "run_copilot", fake_copilot)
    monkeypatch.setattr(ai, "get_settings", lambda: SimpleNamespace(public_access=True))

    with _client() as client:
        response = client.post(
            "/api/ai/copilot/stream",
            json={"message": "x", "graph": {"nodes": [], "edges": [], "id": "g"}},
        )

    events = _frames(response.text)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "exception"
    assert "full message is in the server log" in events[-1]["message"]
    assert "private.example" not in events[-1]["message"]


def test_finish_can_name_a_new_workflow():
    """The canvas starts as "Untitled workflow"; the model's title must win."""
    draft = ScopedDraft(None)
    draft.add_node("prompt", "Step")

    draft.finish(summary="done", name="  Memory   market intel ")

    assert draft.to_graph(name="Untitled workflow").name == "Memory market intel"


def test_a_question_brief_keeps_its_answer():
    """Questions are answered, not turned into an approval-gated edit."""
    from app.ai_buildtime.copilot import _normalise_brief

    brief = _normalise_brief(
        {
            "request_kind": "question",
            "answer": "The Retriever node ranks local documents; it does not search the web.",
            "goal": "explain a node",
        },
        "这个节点是做什么的？",
    )

    assert brief["request_kind"] == "question"
    assert brief["answer"].startswith("The Retriever node")


def test_provider_reasoning_is_read_from_the_message():
    from app.ai_buildtime.gen_node import _message_reasoning

    message = SimpleNamespace(
        additional_kwargs={"reasoning_content": "First check the wiring."},
        response_metadata={},
        content="",
    )

    assert _message_reasoning(message) == "First check the wiring."

    blocks = SimpleNamespace(
        additional_kwargs={},
        response_metadata={},
        content=[{"type": "reasoning", "text": "Plan A"}, {"type": "text", "text": "Done"}],
    )
    assert _message_reasoning(blocks) == "Plan A"


async def test_a_question_returns_an_answer_without_requesting_approval(monkeypatch):
    """The copilot must converse: a question is not an edit plan."""
    from app.ai_buildtime import copilot

    async def fake_json(*args, **kwargs):
        on_thinking = kwargs.get("on_thinking")
        if on_thinking is not None:
            await on_thinking("It is a retrieval node.")
        return {
            "request_kind": "question",
            "answer": "Retriever ranks local documents; it does not search the web.",
            "goal": "explain the node",
            "direction": "answer from the canvas",
        }

    monkeypatch.setattr(copilot, "_complete_json", fake_json)
    monkeypatch.setattr(
        copilot,
        "build_chat_model",
        lambda *args, **kwargs: SimpleNamespace(model_name="deepseek"),
    )
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    result = await copilot.run_copilot(
        "这个节点是做什么的？", _graph(), mode="confirm", on_event=on_event
    )

    assert result["needs_approval"] is False
    assert result["applied"] is False
    assert result["summary"].startswith("Retriever ranks")
    assert any(event["type"] == "thinking" for event in events)
    assert not any(event["type"] == "approval" for event in events)


async def test_an_edit_gets_the_platform_tools_and_plans_without_thinking(monkeypatch):
    """Regression: the plan prompt was called with an unsupported `thinking`.

    That TypeError fired before the edit loop, so every edit-mode copilot
    request failed after the insight step. The same test pins that the platform
    tools (schedule/deploy) are bound alongside the graph tools.
    """
    from app.ai_buildtime import copilot

    class _Reply:
        content = ""
        additional_kwargs: dict = {}
        response_metadata: dict = {}
        usage_metadata = None

        def __init__(self):
            self.tool_calls = [
                {"name": "finish", "args": {"summary": "no change"}, "id": "c1"}
            ]

    class _Model:
        def __init__(self):
            self.bound: list[str] = []

        def bind_tools(self, tools):
            self.bound = [tool["function"]["name"] for tool in tools]
            return self

        async def ainvoke(self, messages):
            return _Reply()

    model = _Model()
    replies = iter(
        [
            {
                "request_kind": "edit",
                "goal": "keep the graph",
                "direction": "no structural change",
                "confidence": "high",
            },
            {
                "diagnosis": "nothing to change",
                "changes": [],
                "steps": [],
                "expected_result": "the graph is unchanged",
            },
        ]
    )

    async def fake_json(*args, **kwargs):
        return next(replies)

    monkeypatch.setattr(copilot, "_complete_json", fake_json)
    monkeypatch.setattr(copilot, "build_chat_model", lambda *a, **kw: model)

    result = await copilot.run_copilot(
        "不要改图，只告诉我现在的限制", _graph(), mode="auto", self_test=False
    )

    # The point is that the edit phase ran at all (and therefore the plan prompt
    # accepted its arguments); a finish with no node changes is not "applied".
    assert result["graph"]["nodes"]
    assert "create_schedule" in model.bound
    assert "deployment_plan" in model.bound
    assert "run_workflow" in model.bound
    assert "get_run_results" in model.bound
    assert "query_database" in model.bound
    assert "test_node" in model.bound
    assert "get_canvas_layout" in model.bound
    assert "list_versions" in model.bound
    assert "get_version" in model.bound
    assert "search_web" in model.bound
    assert "insert_database_rows" in model.bound
    assert "update_database_rows" in model.bound
    assert "delete_database_rows" in model.bound


def test_greetings_are_not_hardcoded():
    """The answer must come from the model, never from a canned phrase table.

    A fixed table also could not tell "你好" from a greeting that carries a
    request, and it made the assistant say the same sentence to everyone.
    """
    from app.ai_buildtime import copilot

    assert not hasattr(copilot, "_casual_reply")
    assert not hasattr(copilot, "_CASUAL_MESSAGES")


async def test_a_greeting_is_answered_by_the_model(monkeypatch):
    """A greeting returns a chat answer with no plan, approval or edit."""
    from app.ai_buildtime import copilot

    answer = "你好！我是画布里的 AI 助手，可以帮你理解或修改这个工作流。"

    async def fake_json(*args, **kwargs):
        return {
            "request_kind": "chat",
            "answer": answer,
            "goal": "greet the user",
            "direction": "",
        }

    monkeypatch.setattr(copilot, "_complete_json", fake_json)
    monkeypatch.setattr(
        copilot,
        "build_chat_model",
        lambda *args, **kwargs: SimpleNamespace(model_name="deepseek"),
    )
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    result = await copilot.run_copilot(
        "你好", _graph(), mode="confirm", on_event=on_event, lang="zh"
    )

    assert result["brief"]["request_kind"] == "chat"
    assert result["summary"] == answer
    assert result["plan"] is None
    assert result["needs_approval"] is False
    assert result["applied"] is False
    assert not any(event["type"] == "approval" for event in events)


def test_deep_thinking_asks_for_a_decision_rationale():
    """The toggle must change the prompt, not only the UI label."""
    from app.ai_buildtime.prompts import build_copilot_insight_messages

    plain = build_copilot_insight_messages(_graph(), "这个节点做什么？", lang="zh")
    deep = build_copilot_insight_messages(
        _graph(), "这个节点做什么？", lang="zh", thinking=True
    )

    assert "Deeper reasoning requested" not in plain[1]["content"]
    assert "Deeper reasoning requested" in deep[1]["content"]
    assert "`reasoning`" in deep[1]["content"]


def test_a_brief_keeps_its_reasoning():
    from app.ai_buildtime.copilot import _normalise_brief

    brief = _normalise_brief(
        {"request_kind": "question", "answer": "It ranks documents.", "reasoning": "Because it calls the retriever."},
        "explain",
    )

    assert brief["reasoning"] == "Because it calls the retriever."


async def test_a_model_reply_reports_its_tokens(monkeypatch):
    """The live token meter reads usage off each authoring reply."""
    from app.ai_buildtime import copilot

    message = SimpleNamespace(
        content='{"ok": true}',
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        additional_kwargs={},
        response_metadata={},
    )

    async def fake_complete(_messages):
        return message

    monkeypatch.setattr(copilot, "_complete_message", fake_complete)
    seen: list[int] = []

    async def on_usage(usage):
        seen.append(usage.total_tokens)

    parsed = await copilot._complete_json(
        [{"role": "user", "content": "x"}], on_usage=on_usage
    )

    assert parsed == {"ok": True}
    assert seen == [150]


async def test_answer_stream_emits_text_before_the_json_closes():
    """The answer must be usable while the model is still writing the object."""
    from app.ai_buildtime.copilot import AnswerStreamer

    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    streamer = AnswerStreamer(emit)
    await streamer.feed('{"request_kind": "chat", "answer": "你好，')
    await streamer.feed('我是画布助手。", "goal": "greet"}')

    assert "".join(chunks) == "你好，我是画布助手。"


async def test_answer_stream_stays_silent_for_an_edit():
    """An edit's JSON also has an `answer` field; it must not leak as a reply."""
    from app.ai_buildtime.copilot import AnswerStreamer

    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    streamer = AnswerStreamer(emit)
    await streamer.feed('{"request_kind": "edit", "answer": "internal note"}')

    assert chunks == []


async def test_complete_json_uses_the_stream_when_asked(monkeypatch):
    from types import SimpleNamespace

    from app.ai_buildtime import copilot

    seen: list[str] = []

    async def fake_stream(messages, *, on_text=None, on_reasoning=None):
        for piece in ('{"request_kind": "chat", "answer": "你', '好"}'):
            if on_text is not None:
                await on_text(piece)
            seen.append(piece)
        return SimpleNamespace(
            content='{"request_kind": "chat", "answer": "你好"}',
            additional_kwargs={},
            response_metadata={},
            usage_metadata=None,
        )

    monkeypatch.setattr(copilot, "_stream_message", fake_stream)
    deltas: list[str] = []

    async def on_text(text: str) -> None:
        deltas.append(text)

    parsed = await copilot._complete_json(
        [{"role": "user", "content": "你好"}], on_text=on_text
    )

    assert parsed["answer"] == "你好"
    assert deltas == ['{"request_kind": "chat", "answer": "你', '好"}']


async def test_reasoning_streams_before_the_answer():
    """Thinking must arrive first; an answer followed by its rationale is backwards."""
    from app.ai_buildtime.copilot import AnswerStreamer

    order: list[tuple[str, str]] = []

    async def answer(text: str) -> None:
        order.append(("answer", text))

    async def reasoning(text: str) -> None:
        order.append(("reasoning", text))

    streamer = AnswerStreamer(answer, reasoning)
    await streamer.feed('{"reasoning": "先看它是入口', )
    await streamer.feed('节点。", "request_kind": "chat", "answer": "它是入口"}')

    kinds = [item[0] for item in order]
    assert kinds == ["reasoning", "reasoning", "answer"]
    assert "".join(text for kind, text in order if kind == "reasoning") == "先看它是入口节点。"
    assert "".join(text for kind, text in order if kind == "answer") == "它是入口"


def test_an_edit_keeps_the_document_id():
    """Every streamed snapshot must stay the same document.

    The canvas keys the open workflow and its conversation by id. A fresh id
    per snapshot reads as a document switch and clears the transcript — which
    is exactly what happened after the copilot created an agent.
    """
    draft = ScopedDraft(_graph(), allowed=None)
    draft.add_node("prompt", "Second", "another prompt", {"template": "x"})
    assert draft.to_graph(name="edited").id == "g"


def test_a_build_from_scratch_gets_an_id():
    draft = ScopedDraft()
    draft.add_node("start", "Start", "", {"input_schema": {"query": ""}})
    graph = draft.to_graph(name="fresh")
    assert graph.id


def test_untrusted_code_is_a_human_gate_not_a_regression():
    untrusted = SimpleNamespace(code="untrusted_node")
    missing = SimpleNamespace(code="missing_required_param")
    assert not _regressed(
        SimpleNamespace(errors=[]), SimpleNamespace(errors=[untrusted])
    )
    assert _regressed(
        SimpleNamespace(errors=[]),
        SimpleNamespace(errors=[untrusted, missing]),
    )
    assert not _regressed(
        SimpleNamespace(errors=[untrusted]),
        SimpleNamespace(errors=[untrusted, untrusted]),
    )


async def test_run_draft_tool_executes_the_current_draft():
    """The copilot must be able to test its own draft, not just describe it."""
    from app.ai_buildtime import copilot

    graph = WorkflowGraph.model_validate(
        {
            "id": "run-me",
            "name": "run me",
            "nodes": [
                {
                    "id": "s",
                    "type": "start",
                    "label": "Start",
                    "position": {"x": 0, "y": 0},
                    "config": {"input_schema": {"q": ""}},
                },
                {
                    "id": "e",
                    "type": "end",
                    "label": "End",
                    "position": {"x": 1, "y": 0},
                    "config": {"output_label": "answer"},
                },
            ],
            "edges": [
                {
                    "id": "x",
                    "source": "s",
                    "target": "e",
                    "sourceHandle": "out",
                    "targetHandle": "in",
                }
            ],
        }
    )
    draft = ScopedDraft(graph)
    events: list[dict] = []

    async def publish(event: dict) -> None:
        events.append(event)

    raw = await copilot.run_draft_tool(draft, graph, {}, publish)
    payload = json.loads(raw)
    assert payload["status"] == "passed"
    assert payload["outputs"] == {"answer": {"q": ""}}
    assert [step["node_id"] for step in payload["steps"]] == ["s", "e"]
    assert events[0] == {"type": "self_test", "phase": "start"}
    assert events[-1]["type"] == "self_test" and events[-1]["phase"] == "done"
    assert events[-1]["result"]["trace"]


def test_read_runs_tool_refuses_another_workflow(monkeypatch):
    from app.ai_buildtime import copilot

    class _Store:
        def summary(self, run_id):
            return {"id": run_id, "workflow_id": "other", "status": "succeeded"}

        def steps(self, run_id):
            return []

        def history(self, workflow_id, limit):
            return []

    monkeypatch.setattr(copilot, "get_run_store", lambda: _Store())
    assert copilot.read_runs_tool("mine", {"run_id": "r1"}).startswith("ERROR")


async def test_query_database_tool_uses_the_registered_read_tools(monkeypatch):
    from app.ai_buildtime import copilot

    calls: list[tuple[str, dict]] = []

    class _Tool:
        def __init__(self, name):
            self.name = name

        async def __call__(self, **kwargs):
            calls.append((self.name, kwargs))
            return {"ok": True, "table": kwargs.get("table", ""), **kwargs}

    class _Registry:
        def get(self, name):
            return _Tool(name)

    monkeypatch.setattr(copilot, "get_tool_registry", lambda: _Registry())

    tables = json.loads(await copilot.query_database_tool({}))
    assert calls[-1][0] == "db_tables"
    assert tables["ok"] is True

    rows = json.loads(
        await copilot.query_database_tool(
            {"table": "results", "limit": 999, "where_column": "status", "where_value": "ok"}
        )
    )
    name, kwargs = calls[-1]
    assert name == "db_query"
    assert kwargs["limit"] == 200
    assert kwargs["where_column"] == "status"
    assert rows["ok"] is True


def test_copilot_insight_attaches_images_as_user_content():
    """The image must be an image_url block, not text the model cannot see.

    The first version of this plumbing wrapped the research prompt instead of
    the insight prompt; the model then answered "I have no image pixels".
    """
    from app.ai_buildtime.prompts import build_copilot_insight_messages

    messages = build_copilot_insight_messages(
        _graph(), "看图", images=["data:image/png;base64,AAAA"]
    )
    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[-1]["type"] == "image_url"
    assert content[-1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_test_node_tool_runs_one_node_and_returns_output():
    from app.ai_buildtime import copilot

    graph = WorkflowGraph.model_validate(
        {
            "id": "one-node",
            "name": "one",
            "nodes": [
                {
                    "id": "s",
                    "type": "start",
                    "label": "Start",
                    "position": {"x": 0, "y": 0},
                    "config": {"input_schema": {"q": ""}},
                }
            ],
            "edges": [],
        }
    )
    draft = ScopedDraft(graph)
    raw = await copilot.test_node_tool(
        draft, {"node_id": "s", "inputs": {"in": {"q": "hello"}}}
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["node_id"] == "s"
    assert "outputs" in payload


def test_canvas_layout_tool_returns_positions():
    from app.ai_buildtime import copilot

    draft = ScopedDraft(_graph())
    payload = json.loads(copilot.canvas_layout_tool(draft))
    by_id = {node["id"]: node for node in payload["nodes"]}
    assert by_id["s"]["x"] == 10
    assert by_id["p"]["y"] == 40
    assert payload["edges"][0]["source"] == "s"


async def test_reasoning_is_decoded_across_escape_boundaries():
    from app.ai_buildtime.copilot import AnswerStreamer

    pieces: list[str] = []

    async def noop(_text: str) -> None:
        return None

    async def reasoning(text: str) -> None:
        pieces.append(text)

    streamer = AnswerStreamer(noop, reasoning)
    await streamer.feed('{"reasoning": "第一行\\')
    await streamer.feed('n第二行", "request_kind": "chat", "answer": "ok"}')

    assert "".join(pieces) == "第一行\n第二行"


def test_a_brief_keeps_its_investigation_flag():
    """The brief decides whether the read-only turn runs before the plan."""
    from app.ai_buildtime.copilot import _normalise_brief

    assert _normalise_brief({"goal": "x"}, "x")["needs_investigation"] is False
    assert (
        _normalise_brief({"goal": "x", "needs_investigation": True}, "x")[
            "needs_investigation"
        ]
        is True
    )


def test_the_pre_plan_tools_are_strictly_read_only():
    """Nothing in the thinking stage may run a workflow, a node, or a write."""
    from app.ai_buildtime import copilot

    names = {spec["function"]["name"] for spec in copilot.investigation_tool_specs()}

    assert names == {
        "query_database",
        "get_run_results",
        "list_versions",
        "get_version",
        "get_canvas_layout",
        "search_web",
    }
    assert not names & {
        "run_workflow",
        "test_node",
        "insert_database_rows",
        "update_database_rows",
        "delete_database_rows",
        "add_node",
        "update_node",
        "delete_node",
    }


def test_the_investigation_prompt_carries_the_request_and_brief():
    from app.ai_buildtime.prompts import build_copilot_investigate_messages

    messages = build_copilot_investigate_messages(
        _graph(),
        "上次运行为什么失败？",
        {"goal": "diagnose the last run", "needs_investigation": True},
    )
    assert "READ-ONLY" in messages[0]["content"]
    assert "get_run_results" in messages[0]["content"]
    assert "上次运行为什么失败？" in messages[1]["content"]
    assert "diagnose the last run" in messages[1]["content"]
    assert "FINDINGS" in messages[0]["content"]


async def test_read_only_evidence_is_gathered_before_the_plan(monkeypatch):
    """A brief that needs live facts produces a tool turn and a plan input.

    This is the user-visible contract: tool calls happen while the agent is
    still in the thinking phase, and the plan sees their real results rather
    than a guess.
    """
    from app.ai_buildtime import copilot, prompts

    class _Reply:
        content = ""
        additional_kwargs: dict = {}
        response_metadata: dict = {}
        usage_metadata = None

        def __init__(self, content: str = "", calls: list | None = None):
            self.content = content
            self.tool_calls = calls or []

    class _Model:
        def __init__(self):
            self.rounds: list[list] = []
            self.bindings: list[list[str]] = []

        def bind_tools(self, tools):
            self.bindings.append([tool["function"]["name"] for tool in tools])
            return self

        async def ainvoke(self, messages):
            self.rounds.append(messages)
            if len(self.rounds) == 1:
                return _Reply(
                    calls=[
                        {
                            "name": "query_database",
                            "args": {"table": "orders"},
                            "id": "c1",
                        }
                    ]
                )
            if len(self.rounds) == 2:
                return _Reply("FINDINGS:\n- orders has 3 rows\nGAPS:\n- none")
            return _Reply()

    model = _Model()
    briefs = iter(
        [
            {
                "request_kind": "edit",
                "goal": "diagnose the last run",
                "direction": "read evidence, then adjust",
                "needs_investigation": True,
            },
            {
                "diagnosis": "no change needed",
                "changes": [],
                "steps": [],
                "expected_result": "unchanged",
            },
        ]
    )

    async def fake_json(*args, **kwargs):
        return next(briefs)

    async def fake_query(_args):
        return "orders: 3 rows"

    captured: dict = {}
    real_plan = prompts.build_copilot_plan_messages

    def spy_plan(*args, **kwargs):
        captured.update(kwargs)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(copilot, "_complete_json", fake_json)
    monkeypatch.setattr(copilot, "build_chat_model", lambda *a, **kw: model)
    monkeypatch.setattr(copilot, "query_database_tool", fake_query)
    monkeypatch.setattr(copilot, "build_copilot_plan_messages", spy_plan)

    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    result = await copilot.run_copilot(
        "上次运行为什么失败？查一下 orders", _graph(), mode="auto", self_test=False,
        on_event=on_event,
    )

    assert set(model.bindings[0]) == {
        "query_database",
        "get_run_results",
        "list_versions",
        "get_version",
        "get_canvas_layout",
        "search_web",
    }
    assert "run_workflow" not in model.bindings[0]
    assert captured["investigation"].startswith("FINDINGS:")
    assert "orders has 3 rows" in captured["investigation"]
    assert any(
        event.get("type") == "tool" and event.get("tool") == "query_database"
        for event in events
    )
    assert any(
        event.get("type") == "stage"
        and event.get("stage") == "investigate"
        and event.get("status") == "done"
        for event in events
    )
    assert result["investigation"].startswith("FINDINGS:")


async def test_run_python_tool_computes_in_the_sandbox():
    """The copilot can execute a calculation without adding a Code node."""
    from app.ai_buildtime import copilot

    raw = await copilot.run_python_tool({"code": "result = sum(range(5))"})
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["result"] == 10
    assert payload["error"] is None


async def test_run_python_tool_refuses_in_the_public_demo(monkeypatch):
    from app.ai_buildtime import copilot

    monkeypatch.setattr(
        copilot, "get_settings", lambda: SimpleNamespace(public_demo=True)
    )

    raw = await copilot.run_python_tool({"code": "result = 1"})

    assert raw.startswith("ERROR: code execution is disabled")


def test_the_public_demo_does_not_bind_run_python(monkeypatch):
    from app.ai_buildtime import copilot

    monkeypatch.setattr(
        copilot, "get_settings", lambda: SimpleNamespace(public_demo=True)
    )
    names = {spec["function"]["name"] for spec in copilot.execution_tool_specs()}

    assert "run_python" not in names
    assert "query_database" in names
