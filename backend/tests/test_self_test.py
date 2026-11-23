"""Trial-run behaviour for generated workflows.

These exercise the engine rather than a model: the point of the self-test is
that a graph which validates still has to *run*, so the failures here are real
node failures produced by real nodes.
"""

from __future__ import annotations

import asyncio

from app.ai_buildtime.self_test import looks_environmental, trial_feedback, trial_run
from app.graph.schema import WorkflowGraph


def _graph(nodes, edges):
    return WorkflowGraph(nodes=nodes, edges=edges)


def _node(node_id: str, node_type: str, **kw):
    payload = {"id": node_id, "type": node_type, "label": node_id}
    payload.update(kw)
    return payload


def _edge(source: str, target: str, source_handle: str | None = None):
    return {
        "id": f"{source}-{target}",
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": "in",
    }


def _passing_graph():
    return _graph(
        [
            _node("s", "start", config={"input_schema": {"query": "hello"}}),
            _node("e", "end"),
        ],
        [_edge("s", "e")],
    )


def _failing_graph():
    return _graph(
        [
            _node("s", "start", config={"input_schema": {"query": "hello"}}),
            _node(
                "p",
                "prompt",
                config={
                    "template": "{{ inputs.not_a_real_field }}",
                    "missing": "error",
                },
            ),
            _node("e", "end"),
        ],
        [_edge("s", "p"), _edge("p", "e")],
    )


def test_a_valid_graph_passes_its_trial_run():
    result = trial_run_sync(_passing_graph())
    assert result["status"] == "passed"
    assert result["ok"] is True
    assert result["error"] is None


def test_a_node_failure_is_reported_with_its_node():
    result = trial_run_sync(_failing_graph())
    assert result["status"] == "failed"
    assert result["ok"] is False
    assert result["failed_node"] == "p"
    assert "not_a_real_field" in (result["error"] or "")


def test_an_untrusted_code_node_is_skipped_not_executed():
    graph = _graph(
        [
            _node("s", "start"),
            _node("c", "code", config={"code": "print('x')"}),
            _node("e", "end"),
        ],
        [_edge("s", "c"), _edge("c", "e")],
    )
    result = trial_run_sync(graph)
    assert result["status"] == "skipped"
    assert result["reason"] == "compile_error"
    assert "trust" in (result["error"] or "").lower()


def test_environment_errors_are_classified():
    assert looks_environmental("ConnectError: connection refused")
    assert looks_environmental("HTTP 429 rate limit")
    assert not looks_environmental(
        "Template referenced values that are not available: inputs.x"
    )


def test_feedback_names_the_failing_node_and_the_error():
    result = trial_run_sync(_failing_graph())
    feedback = trial_feedback(result)
    assert "Failing node: p" in feedback
    assert "not_a_real_field" in feedback
    assert "call `finish` again" in feedback


# ---------------------------------------------------------------------------
# Wiring: a generation whose graph validates but fails at run time must be
# handed back to the model, and the loop must stop at its own bound.
# ---------------------------------------------------------------------------


class _FinishModel:
    """A model that only ever says 'done', ignoring every correction."""

    model_name = "fake-tool-model"

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ANN001 - matches the LangChain API
        return self

    async def ainvoke(self, messages):  # noqa: ANN001
        from langchain_core.messages import AIMessage

        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "finish", "args": {"summary": "done"}, "id": f"c{self.calls}"}
            ],
        )


def test_generation_reruns_after_a_runtime_failure(monkeypatch):
    from app.ai_buildtime import gen_graph

    model = _FinishModel()
    monkeypatch.setattr(gen_graph, "build_chat_model", lambda *a, **kw: model)

    result = asyncio.run(
        gen_graph.generate_graph(
            "build it",
            existing=_failing_graph(),
            max_iterations=2,
        )
    )

    assert result["test"]["status"] == "failed"
    assert result["test_corrections"] == 2
    # Three finishes: the first build plus one after each of the two corrections.
    assert model.calls == 3
    assert sum(1 for e in result["transcript"] if e["tool"] == "self_test") == 3


class _ScriptedModel:
    """Calls a fixed list of tools, one per round."""

    model_name = "fake-tool-model"

    def __init__(self, script) -> None:
        self.script = script
        self.index = 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    async def ainvoke(self, messages):  # noqa: ANN001
        from langchain_core.messages import AIMessage

        tool, args = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return AIMessage(
            content="",
            tool_calls=[{"name": tool, "args": args, "id": f"c{self.index}"}],
        )


def test_connect_stores_the_edge_purpose_and_inspect_shows_it():
    from app.ai_buildtime.gen_graph import GraphDraft, draft_tools

    draft = GraphDraft()
    draft.add_node("start", "Start")
    draft.add_node("end", "End")
    source, target = [node.id for node in draft.nodes]

    draft.connect(source, target, label="用户问题")

    assert draft.edges[0].label == "用户问题"
    assert "用户问题" in draft.inspect_canvas()

    connect = next(
        tool for tool in draft_tools() if tool["function"]["name"] == "connect"
    )
    assert "label" in connect["function"]["parameters"]["required"]


def test_generation_publishes_a_graph_snapshot_after_each_mutation(monkeypatch):
    from app.ai_buildtime import gen_graph

    model = _ScriptedModel(
        [
            (
                "update_node",
                {"node_id": "s", "config": {"input_schema": {"query": "hi"}}},
            ),
            ("finish", {"summary": "done"}),
        ]
    )
    monkeypatch.setattr(gen_graph, "build_chat_model", lambda *a, **kw: model)

    events: list[dict] = []

    async def on_event(event):
        events.append(event)

    asyncio.run(
        gen_graph.generate_graph(
            "build it",
            existing=_failing_graph(),
            max_iterations=3,
            self_test=False,
            on_event=on_event,
        )
    )

    assert [e["type"] for e in events[:2]] == ["status", "tool"]
    snapshots = [e for e in events if e["type"] == "graph"]
    assert len(snapshots) == 1
    # Position changes are deliberately not "updates": layout() reflows the
    # canvas on every add, and a highlight that covers everything says nothing.
    assert snapshots[0]["changed"]["updated_nodes"] == ["s"]


def test_generation_can_skip_the_trial_run(monkeypatch):
    from app.ai_buildtime import gen_graph

    model = _FinishModel()
    monkeypatch.setattr(gen_graph, "build_chat_model", lambda *a, **kw: model)

    result = asyncio.run(
        gen_graph.generate_graph(
            "build it",
            existing=_failing_graph(),
            max_iterations=2,
            self_test=False,
        )
    )

    assert result["test"] is None
    assert result["self_test"] is False
    assert model.calls == 1


# ---------------------------------------------------------------------------
# The trial run is async; pytest here has no async plugin, so drive it directly.
# ---------------------------------------------------------------------------


def trial_run_sync(graph):
    return asyncio.run(trial_run(graph))
