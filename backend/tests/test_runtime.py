"""End-to-end runtime tests.

These exercise the real execution path — compile, run, stream, resume — against
the mock chat model, so they assert on behaviour rather than on mocks of our own
code.
"""

from __future__ import annotations

import asyncio

import pytest

from app.graph.schema import RunCreateRequest, RunEventType, RunStatus, WorkflowGraph
from app.runtime.manager import RunManager


async def _run_to_completion(manager: RunManager, context, *, allow_failure: bool = False) -> list:
    """Start a run and collect every event it emits.

    Raises with the run's error unless ``allow_failure`` is set — without this,
    a broken run surfaces only as ``RunStatus.FAILED`` and every diagnosis starts
    from scratch.
    """
    events = []

    async def consume():
        async for event in context.bus.subscribe():
            events.append(event)

    collector = asyncio.create_task(consume())
    await manager.start(context)
    assert context.task is not None
    await context.task
    await collector

    if context.status is RunStatus.FAILED and not allow_failure:
        raise AssertionError(f"run failed unexpectedly: {context.error}")
    return events


def _node(node_id, node_type, **kw):
    payload = {"id": node_id, "type": node_type, "label": node_id}
    payload.update(kw)
    return payload


def _edge(source, target, source_handle=None, target_handle="in", edge_id=None):
    return {
        "id": edge_id or f"{source}-{target}",
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": target_handle,
    }


@pytest.fixture
def manager() -> RunManager:
    return RunManager()


class TestLinearRun:
    async def test_prompt_only_workflow_produces_output(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"query": "hello"}}),
                _node(
                    "tpl",
                    "prompt",
                    config={"template": "Answer: {{ vars.s.out.query }}"},
                ),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "tpl"), _edge("tpl", "e")],
        )

        context = await manager.create(
            "wf1", graph, RunCreateRequest(workflow_id="wf1", inputs={"query": "world"})
        )
        events = await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "Answer: world"

    async def test_emits_lifecycle_events_per_node(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"q": "x"}}),
                _node("p", "prompt", config={"template": "{{ vars.s.out.q }}"}),
                _node("e", "end"),
            ],
            edges=[_edge("s", "p"), _edge("p", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        events = await _run_to_completion(manager, context)

        kinds = [e.type for e in events]
        assert kinds[0] is RunEventType.RUN_STARTED
        assert kinds[-1] is RunEventType.RUN_FINISHED

        started_nodes = {e.node_id for e in events if e.type is RunEventType.NODE_STARTED}
        assert started_nodes == {"s", "p", "e"}

        # Every started node must also finish.
        finished_nodes = {e.node_id for e in events if e.type is RunEventType.NODE_FINISHED}
        assert started_nodes == finished_nodes

    async def test_records_one_step_per_node_in_order(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": "x"}),
                _node("e", "end"),
            ],
            edges=[_edge("s", "p"), _edge("p", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.step_order == ["s", "p", "e"]
        assert context.steps["p"].status == "succeeded"
        assert context.steps["p"].outputs  # the rendered prompt is recorded

    async def test_later_subscriber_replays_history(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("e", "end"),
            ],
            edges=[_edge("s", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        # Subscribe *after* completion — the buffer must still serve the run.
        replayed = [event async for event in context.bus.subscribe()]
        assert replayed
        assert replayed[-1].type is RunEventType.RUN_FINISHED

        # And `after` must skip what the client already saw.
        partial = [event async for event in context.bus.subscribe(after=replayed[0].seq)]
        assert all(event.seq > replayed[0].seq for event in partial)


class TestFailure:
    async def test_invalid_workflow_fails_at_creation(self, manager):
        """A broken graph should fail the HTTP call, not the background task."""
        from app.runtime.manager import RunError

        graph = WorkflowGraph(
            id="wf1",
            nodes=[_node("p", "prompt", config={"template": "x"})],  # no Start
            edges=[],
        )
        with pytest.raises(RunError, match="Start"):
            await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))

    async def test_missing_template_placeholder_fails_the_run(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"q": "x"}}),
                _node(
                    "p",
                    "prompt",
                    config={"template": "{{ nope }}", "missing": "error"},
                ),
            ],
            edges=[_edge("s", "p")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        events = await _run_to_completion(manager, context, allow_failure=True)
        assert context.status is RunStatus.FAILED
        assert context.error is not None
        assert events[-1].type is RunEventType.RUN_FAILED
        # The node must be identifiable from the failure.
        failed = [e for e in events if e.type is RunEventType.NODE_FAILED]
        assert failed and failed[0].node_id == "p"

    async def test_node_error_names_the_node(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("c", "code", config={"code": "raise ValueError('boom')"}, trusted=True),
            ],
            edges=[_edge("s", "c")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context, allow_failure=True)
        assert context.status is RunStatus.FAILED
        assert "boom" in (context.error or "")


class TestConditionRouting:
    async def test_true_branch_runs_and_the_other_does_not(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"n": 5}}),
                _node(
                    "c",
                    "condition",
                    config={
                        "branches": [
                            {"id": "big", "label": "Big", "expression": "vars.s.out.n > 3"},
                            {"id": "small", "label": "Small", "expression": ""},
                        ]
                    },
                ),
                _node("yes", "prompt", config={"template": "BIG"}),
                _node("no", "prompt", config={"template": "SMALL"}),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[
                _edge("s", "c"),
                _edge("c", "yes", source_handle="big"),
                _edge("c", "no", source_handle="small"),
                _edge("yes", "e"),
            ],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "BIG"
        assert "yes" in context.steps
        assert "no" not in context.steps, "the untaken branch must not execute"

    async def test_fallback_branch_is_taken_when_nothing_matches(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"n": 1}}),
                _node(
                    "c",
                    "condition",
                    config={
                        "branches": [
                            {"id": "big", "label": "Big", "expression": "vars.s.out.n > 100"},
                            {"id": "other", "label": "Other", "expression": ""},
                        ]
                    },
                ),
                _node("a", "prompt", config={"template": "BIG"}),
                _node("b", "prompt", config={"template": "FALLBACK"}),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[
                _edge("s", "c"),
                _edge("c", "a", source_handle="big"),
                _edge("c", "b", source_handle="other"),
                _edge("b", "e"),
            ],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.outputs["result"] == "FALLBACK"


class TestLoop:
    async def test_loop_stops_at_its_ceiling(self, manager):
        """A condition that never goes false must still terminate."""
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node(
                    "l",
                    "loop",
                    config={
                        "max_iterations": 3,
                        "condition": "iteration < 99",  # never satisfied on its own
                        "on_max": "exit",
                    },
                ),
                _node("body", "prompt", config={"template": "step {{ vars.l.out }}"}),
                _node("e", "end"),
            ],
            edges=[
                _edge("s", "l"),
                _edge("l", "body", source_handle="continue"),
                _edge("body", "l"),  # the back edge that makes it a loop
                _edge("l", "e", source_handle="done"),
            ],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED
        assert "l" in context.steps
        # The ceiling must be respected exactly, not overshot.
        assert context.steps["l"].outputs["iteration"] == 3

    async def test_condition_ending_the_loop_exits_early(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node(
                    "l",
                    "loop",
                    config={"max_iterations": 10, "condition": "iteration < 2"},
                ),
                _node("body", "prompt", config={"template": "step"}),
                _node("e", "end"),
            ],
            edges=[
                _edge("s", "l"),
                _edge("l", "body", source_handle="continue"),
                _edge("body", "l"),
                _edge("l", "e", source_handle="done"),
            ],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED
        # Stopped on the condition (iteration 2), well below the ceiling of 10.
        assert context.steps["l"].outputs["iteration"] == 2

    async def test_loop_ceiling_can_fail_the_run(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node(
                    "l",
                    "loop",
                    config={
                        "max_iterations": 2,
                        "condition": "iteration < 99",
                        "on_max": "error",
                    },
                ),
                _node("body", "prompt", config={"template": "step"}),
            ],
            edges=[
                _edge("s", "l"),
                _edge("l", "body", source_handle="continue"),
                _edge("body", "l"),
            ],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context, allow_failure=True)

        assert context.status is RunStatus.FAILED
        assert "ceiling" in (context.error or "").lower()


class TestCodeNode:
    async def test_transforms_input(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"q": "abc"}}),
                _node(
                    "c",
                    "code",
                    trusted=True,
                    config={"code": "result = inputs['in']['q'].upper()"},
                ),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "c"), _edge("c", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "ABC"

    async def test_untrusted_code_node_is_rejected_before_running(self, manager):
        from app.runtime.manager import RunError

        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("c", "code", config={"code": "result = 1"}),
            ],
            edges=[_edge("s", "c")],
        )
        with pytest.raises(RunError, match="Trusted"):
            await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))


class TestAggregator:
    async def test_merges_a_single_active_branch(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"n": 9}}),
                _node(
                    "c",
                    "condition",
                    config={
                        "branches": [
                            {"id": "big", "label": "Big", "expression": "vars.s.out.n > 5"},
                            {"id": "small", "label": "Small", "expression": ""},
                        ]
                    },
                ),
                _node("a", "prompt", config={"template": "BIGPATH"}),
                _node("b", "prompt", config={"template": "SMALLPATH"}),
                _node("agg", "aggregator", config={"mode": "first_non_null"}),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[
                _edge("s", "c"),
                _edge("c", "a", source_handle="big"),
                _edge("c", "b", source_handle="small"),
                _edge("a", "agg"),
                _edge("b", "agg"),
                _edge("agg", "e"),
            ],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "BIGPATH"


class TestHumanInTheLoop:
    async def test_run_pauses_then_resumes_with_a_value(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node(
                    "h",
                    "human_input",
                    config={
                        "message": "Approve?",
                        "fields": {"value": {"type": "string"}},
                    },
                ),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "h"), _edge("h", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        events = await _run_to_completion(manager, context)

        assert context.status is RunStatus.INTERRUPTED
        assert context.interrupt is not None
        assert context.interrupt.get("message") == "Approve?"
        assert any(e.type is RunEventType.INTERRUPT_RAISED for e in events)

        # Resume with the reviewer's answer.
        await manager.resume(context.id, {"approved": True, "values": {"value": "ok"}})
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.SUCCEEDED
        # The node returns the named fields the reviewer filled in, not a bare
        # value — downstream nodes read `vars.<id>.out.value`.
        assert context.outputs["result"] == {"value": "ok"}

    async def test_resuming_a_run_that_is_not_paused_is_rejected(self, manager):
        from app.runtime.manager import RunError

        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("e", "end"),
            ],
            edges=[_edge("s", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        with pytest.raises(RunError, match="not awaiting input"):
            await manager.resume(context.id, "x")


class TestRunInputsAreReadableByName:
    """`{{ query }}` resolves to the `query` this run was started with.

    The pairing the product ships is Start declaring `query` and four node types
    defaulting their prompt to `{{ query }}`, so this has to work. Measured
    2026-10-09: it did not. A run's inputs live in the graph's `inputs` *channel*,
    and `template_scope` deliberately does not merge channels by name — merging
    `inputs` once replaced the node's own handles and broke `{{ inputs.in }}` —
    so the default prompt rendered empty and the model was handed an empty user
    message. The canvas browser test recorded both symptoms: "Unresolved
    placeholders rendered as empty: query" in single-node debugging, and a full
    run whose reply ignored the question.

    Both halves are asserted, because fixing one by breaking the other is the
    tempting move: run inputs are readable by name, *and* the node's own handles
    still win over a run input with the same name.
    """

    async def test_the_default_prompt_reaches_the_model(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"query": ""}}),
                _node("l", "llm", config={"prompt": "{{ query }}"}),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "l"), _edge("l", "e")],
        )
        context = await manager.create(
            "wf1", graph, RunCreateRequest(workflow_id="wf1", inputs={"query": "2+2 等于几"})
        )
        events = await _run_to_completion(manager, context)

        # The mock model echoes the last message it was sent, so the answer *is*
        # the prompt. An unresolved placeholder leaves the system prompt as the
        # last non-empty message, and the echo names that instead.
        assert "2+2 等于几" in context.outputs["result"]

        unresolved = [
            e.data.get("message")
            for e in events
            if e.type is RunEventType.LOG and "Unresolved" in str(e.data.get("message"))
        ]
        assert unresolved == [], unresolved

    async def test_a_handle_still_wins_over_a_run_input_of_the_same_name(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"in": "from-run"}}),
                _node("tpl", "prompt", config={"template": "{{ inputs.in }}", "missing": "error"}),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "tpl"), _edge("tpl", "e")],
        )
        context = await manager.create(
            "wf1",
            graph,
            # `inputs` as a key too: reserved names must not be clobbered by an
            # input that happens to be called `inputs`.
            RunCreateRequest(workflow_id="wf1", inputs={"in": "from-run", "inputs": "decoy"}),
        )
        await _run_to_completion(manager, context)

        # The handle carries the upstream payload — a dict — not the run input of
        # the same name. `missing: error` would have failed the run if the
        # placeholder had been shadowed into something unresolvable.
        rendered = context.outputs["result"]
        assert rendered.startswith("{"), rendered
        assert "from-run" in rendered


class TestLLMNode:
    async def test_mock_model_streams_chunks(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"query": "hi"}}),
                _node("l", "llm", config={"prompt": "{{ query }}"}),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "l"), _edge("l", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        events = await _run_to_completion(manager, context)

        assert context.status is RunStatus.SUCCEEDED

        chunks = [e for e in events if e.type is RunEventType.NODE_OUTPUT_CHUNK]
        assert chunks, "expected streamed token chunks"
        assert all(e.node_id == "l" for e in chunks)
        # Chunks carry growing text so the UI can render progressively.
        assert "text" in chunks[-1].data
        assert len(chunks[-1].data["text"]) > 0

    async def test_json_output_is_parsed(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {"query": "hi"}}),
                _node(
                    "l",
                    "llm",
                    config={"prompt": "{{ query }}", "output_format": "json"},
                ),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "l"), _edge("l", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        # The mock model replies with prose, so JSON parsing legitimately falls
        # back to the raw string — the point is that it does not crash.
        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"]


class TestPersistence:
    async def test_run_and_steps_are_persisted(self, manager):
        from sqlmodel import select

        from app.db.models import Run as RunRow
        from app.db.models import RunStep as RunStepRow
        from app.db.session import create_all, session_scope

        create_all()

        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": "hi"}),
                _node("e", "end"),
            ],
            edges=[_edge("s", "p"), _edge("p", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        with session_scope() as session:
            row = session.get(RunRow, context.id)
            assert row is not None
            assert row.status == "succeeded"

            steps = session.exec(
                select(RunStepRow).where(RunStepRow.run_id == context.id)
            ).all()
            assert len(steps) == 3

    async def test_transcript_is_written(self, manager):
        graph = WorkflowGraph(
            id="wf1",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node("e", "end"),
            ],
            edges=[_edge("s", "e")],
        )
        context = await manager.create("wf1", graph, RunCreateRequest(workflow_id="wf1"))
        await _run_to_completion(manager, context)

        assert context.transcript_path is not None
        import json
        from pathlib import Path

        payload = json.loads(Path(context.transcript_path).read_text(encoding="utf-8"))
        assert payload["run"]["status"] == "succeeded"
        assert payload["events"]
