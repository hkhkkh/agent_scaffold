"""Naming the run's answer.

An End node returns ``{"<output_label>": value}``, so the *name* of the result is
already a key on the payload — nothing needs to record it a second time. What was
missing is one function that agrees on which key that is. The canvas guessed with
a hardcoded list (``final_ecommerce_solution``, then ``result``), so any workflow
whose label fell outside that list rendered no result at all.

Two properties are worth defending here:

* **Ambiguous payloads return ``None`` rather than a guess.** No candidates, or
  several, means there is no answer — and a wrong pick is worse than a blank,
  because it looks like the agent said something it never said.
* **The derivation reads what the client receives.** Both read paths derive it
  from the *redacted* dict, so the pinned result cannot become a second read path
  with a masking rule of its own. ``test_run_store`` compares the two summary
  key sets; these tests pin the value, which that comparison cannot see.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.graph.schema import RunCreateRequest, RunStatus, WorkflowGraph
from app.runtime.manager import RunManager
from app.runtime.runs import NullRunStore, set_run_store
from app.runtime.serialize import final_output, redact

# ---------------------------------------------------------------------------
# 1. The pure function
# ---------------------------------------------------------------------------


class TestPickingTheAnswer:
    def test_a_single_key_is_the_answer(self):
        assert final_output({"memo": "hello"}) == {"key": "memo", "value": "hello"}

    def test_the_state_dump_is_not_the_answer(self):
        """``include_state`` writes a debugging dump beside the result."""
        outputs = {"memo": "hello", "state": {"vars": {"a": 1}, "inputs": {}}}
        assert final_output(outputs) == {"key": "memo", "value": "hello"}

    def test_a_state_only_payload_has_no_answer(self):
        assert final_output({"state": {"vars": {}}}) is None

    def test_a_null_answer_is_still_an_answer(self):
        """``{"result": None}`` is a run that produced nothing, not a missing key."""
        assert final_output({"result": None}) == {"key": "result", "value": None}

    def test_two_candidates_are_not_guessed_between(self):
        """Two End nodes, or a hand-written payload: picking one would be a lie."""
        assert final_output({"a": 1, "b": 2}) is None

    def test_nothing_at_all_is_no_answer(self):
        assert final_output({}) is None
        assert final_output(None) is None
        assert final_output("not a dict") is None

    def test_the_key_survives_redaction(self):
        """Redaction masks the *value* under a secret-looking key, not the key.

        Worth pinning because both read paths derive from the masked dict: if
        masking ever rewrote key names, the pinned result would come back under
        the wrong name — or none.
        """
        masked = redact({"api_key": "sk-abcdefghijklmnopqrstuvwxyz012345"})
        assert final_output(masked) == {"key": "api_key", "value": masked["api_key"]}


class TestThePartialMarker:
    """A run stopped before an End node reports per-node outputs, not a result.

    ``RunManager._capture_partial_output`` writes ``{"partial": True, "vars":
    {...}}``. The candidate rule already rejects that shape — it has two keys —
    so the marker branch is **defensive, not load-bearing today**. It is kept
    because it states the intent in one readable line, and it is what stays
    correct if that payload ever loses its second key. The lone-marker test below
    is the one that pins the branch itself.
    """

    def test_a_real_partial_dump_is_not_an_answer(self):
        assert final_output({"partial": True, "vars": {"n1": {"out": 1}}}) is None

    def test_a_lone_partial_marker_is_not_an_answer(self):
        """Pins the marker rule rather than the candidate count."""
        assert final_output({"partial": True}) is None

    def test_a_false_marker_is_an_ordinary_key(self):
        """``output_label="partial"`` with a false value is a real result.

        This is why the check is ``is True`` rather than truthiness — a name is
        not a meaning.
        """
        assert final_output({"partial": False}) == {"key": "partial", "value": False}


# ---------------------------------------------------------------------------
# 2. Both read paths
# ---------------------------------------------------------------------------


def _answering_graph(output_label: str = "memo", workflow_id: str = "wf-final") -> WorkflowGraph:
    def node(node_id: str, node_type: str, **config: Any) -> dict[str, Any]:
        return {"id": node_id, "type": node_type, "label": node_id, "config": config}

    def edge(source: str, target: str) -> dict[str, Any]:
        return {
            "id": f"{source}->{target}",
            "source": source,
            "sourceHandle": "out",
            "target": target,
            "targetHandle": "in",
        }

    return WorkflowGraph(
        id=workflow_id,
        nodes=[
            node("start", "start", input_schema={}),
            node("p0", "prompt", template="the answer"),
            node("end", "end", output_label=output_label),
        ],
        edges=[edge("start", "p0"), edge("p0", "end")],
    )


@pytest.fixture(autouse=True)
def _restore_store():
    """The store is process-wide, so every test here must put it back."""
    set_run_store(NullRunStore())
    yield
    set_run_store(None)


class TestBothReadPathsCarryIt:
    async def test_a_live_summary_carries_it(self):
        from app.api.runs import _run_summary

        manager = RunManager()
        graph = _answering_graph()
        context = await manager.create(
            graph.id,
            graph,
            RunCreateRequest(workflow_id=graph.id, inputs={}, recursion_limit=200),
        )
        await manager.start(context)
        assert context.task is not None
        await context.task
        assert context.status is RunStatus.SUCCEEDED

        pinned = _run_summary(context)["final_output"]
        assert pinned is not None, "a finished run reports no answer"
        assert pinned["key"] == "memo"

    async def test_the_label_is_read_from_the_graph_not_assumed(self):
        """A different ``output_label`` comes back under its own name.

        This is the assertion the canvas used to fail: it hardcoded the key, so
        anything else showed nothing.
        """
        from app.api.runs import _run_summary

        manager = RunManager()
        graph = _answering_graph(output_label="audit_report", workflow_id="wf-label")
        context = await manager.create(
            graph.id,
            graph,
            RunCreateRequest(workflow_id=graph.id, inputs={}, recursion_limit=200),
        )
        await manager.start(context)
        assert context.task is not None
        await context.task

        pinned = _run_summary(context)["final_output"]
        assert pinned is not None
        assert pinned["key"] == "audit_report"

    def test_a_persisted_row_derives_it_too(self):
        """The stored path derives from the row, not from a stored column.

        No session and no migration: this is the reason the value is derived
        rather than persisted.
        """
        from app.db.models import Run as RunRow
        from app.db.run_store import _row_to_payload

        row = RunRow(
            id="r1",
            workflow_id="w1",
            status="succeeded",
            outputs={"memo": "hello"},
        )
        assert _row_to_payload(row)["final_output"] == {"key": "memo", "value": "hello"}
