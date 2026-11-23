"""Sub-workflows: a saved workflow used as one step inside another.

Kept deliberately small. The behaviour that matters is "a reference runs the
sub-flow and its result comes out the other side", plus the two ways a reference
can silently do the wrong thing (a stale pin, and a reference loop).
"""

from __future__ import annotations

import pytest

from app.graph.compiler import CompileError, compile_graph
from app.graph.schema import EdgeSpec, NodeSpec, WorkflowGraph
from app.graph.subgraphs import (
    SubgraphError,
    clear_subgraph_provider,
    definition_digest,
    expand_subgraphs,
    pin_references,
    set_subgraph_provider,
)
from app.graph.validate import validate_graph


@pytest.fixture(autouse=True)
def _restore_provider():
    yield
    clear_subgraph_provider()


def _n(node_id: str, node_type: str, **config) -> NodeSpec:
    return NodeSpec(id=node_id, type=node_type, label=node_id, config=config)


def _e(source: str, target: str) -> EdgeSpec:
    return EdgeSpec(
        id=f"{source}->{target}",
        source=source,
        sourceHandle="out",
        target=target,
        targetHandle="in",
    )


def _inner() -> WorkflowGraph:
    """start -> prompt -> end. Reads its own input the way a user would."""
    return WorkflowGraph(
        id="wf-inner",
        nodes=[
            _n("start", "start", input_schema={"q": "default"}),
            _n("tpl", "prompt", template="INNER[{{ vars.start.out.q }}]"),
            _n("end", "end", output_label="result"),
        ],
        edges=[_e("start", "tpl"), _e("tpl", "end")],
    )


def _outer() -> WorkflowGraph:
    return WorkflowGraph(
        id="wf-outer",
        nodes=[
            _n("start", "start", input_schema={"q": "from-outer"}),
            _n("h", "subgraph", ref="wf-inner"),
            _n("after", "prompt", template="AFTER[{{ vars.h.out }}]"),
            _n("end", "end", output_label="result"),
        ],
        edges=[_e("start", "h"), _e("h", "after"), _e("after", "end")],
    )


@pytest.fixture
def _store(monkeypatch):
    """A workflow store holding `_inner()`, as a dict."""
    inner = _inner()
    set_subgraph_provider(lambda wid: inner if wid == "wf-inner" else None)
    return inner


class TestAReferenceRuns:
    async def test_the_sub_flow_runs_and_its_result_reaches_the_next_node(self, _store):
        result = await compile_graph(_outer()).ainvoke({"inputs": {"q": "from-outer"}})
        assert result["output"] == {"result": "AFTER[INNER[from-outer]]"}

    def test_inner_node_ids_are_namespaced_so_they_cannot_collide(self, _store):
        """Both graphs have a `start`; the expanded graph must have exactly one."""
        expanded = expand_subgraphs(_outer())
        ids = [node.id for node in expanded.nodes]
        assert ids == ["start", "h", "h/tpl", "after", "end"]
        assert ids.count("start") == 1

    def test_expanding_twice_is_the_same_as_expanding_once(self, _store):
        """The referencing node survives expansion, so a second pass would inline
        the sub-flow again and produce duplicate ids."""
        once = expand_subgraphs(_outer())
        assert [n.id for n in expand_subgraphs(once).nodes] == [n.id for n in once.nodes]

    def test_a_reference_to_nothing_is_refused_with_a_named_error(self):
        graph = _outer()
        graph.nodes[1].config["ref"] = "wf-gone"
        with pytest.raises(CompileError, match="wf-gone"):
            compile_graph(graph)


class TestTheTwoWaysAReferenceGoesWrong:
    def test_a_edited_sub_flow_is_refused_rather_than_silently_used(self, _store):
        """The whole point of pinning: reusing a sub-flow must not mean that
        editing it changes what every graph using it does, with no warning."""
        saved = pin_references(_outer())
        assert saved.nodes[1].config["ref_hash"] == definition_digest(_store)

        _store.nodes[1].config["template"] = "CHANGED"
        with pytest.raises(CompileError, match="edited since"):
            compile_graph(saved)

    def test_a_reference_loop_is_refused_by_name_not_by_stack_overflow(self):
        a = WorkflowGraph(
            id="wf-a",
            nodes=[_n("start", "start"), _n("h", "subgraph", ref="wf-b"), _n("end", "end")],
            edges=[_e("start", "h"), _e("h", "end")],
        )
        b = WorkflowGraph(
            id="wf-b",
            nodes=[_n("start", "start"), _n("h", "subgraph", ref="wf-a"), _n("end", "end")],
            edges=[_e("start", "h"), _e("h", "end")],
        )
        set_subgraph_provider(lambda wid: {"wf-a": a, "wf-b": b}.get(wid))
        with pytest.raises(SubgraphError, match="wf-a -> wf-b -> wf-a"):
            expand_subgraphs(a)

    def test_a_broken_reference_is_reported_by_the_validator_too(self, _store):
        """So the canvas says so next to the node, not as a 422 after Run."""
        graph = _outer()
        graph.nodes[1].config["ref"] = "wf-gone"
        report = validate_graph(graph)
        assert "subflow_missing_workflow" in {i.code for i in report.errors}


class TestWhatTheDigestCovers:
    """The digest decides what counts as "the definition changed".

    Pinning is only as good as this boundary. Too narrow and a real edit goes
    unnoticed; too wide and moving a node on the canvas looks like an edit, so
    every graph referencing that sub-flow is refused — a false alarm that trains
    people to ignore the error. Task 28 states the requirement directly:
    "挪一下节点不算改定义".
    """

    def test_moving_a_node_does_not_change_the_digest(self):
        """★ Reverse-validated: this test **failed** before `position` was
        excluded, and the failure was a real usability defect, not a theoretical
        one. `model_dump(exclude=...)` with a string name only reaches *top-level*
        fields, and `position` lives inside each node — so dragging a node in the
        sub-workflow made every parent graph report "edited since you pinned it".
        """
        original = _inner()
        moved = _inner()
        for node in moved.nodes:
            node.position.x += 120
            node.position.y += 40

        assert definition_digest(moved) == definition_digest(original)

    def test_a_config_change_does_change_the_digest(self):
        """The other half of the boundary: a real edit must still be caught."""
        original = _inner()
        edited = _inner()
        edited.nodes[1].config["template"] = "DIFFERENT"

        assert definition_digest(edited) != definition_digest(original)

    def test_a_new_node_changes_the_digest(self):
        extended = _inner()
        extended.nodes.append(_n("extra", "prompt", template="EXTRA"))

        assert definition_digest(extended) != definition_digest(_inner())

    def test_adding_an_edge_changes_the_digest(self):
        """Wiring is behaviour: a node that stops being reachable is an edit."""
        original = _inner()
        rewired = _inner()
        rewired.edges = [_e("start", "end")]

        assert definition_digest(rewired) != definition_digest(original)

    def test_the_canvas_viewport_does_not_change_the_digest(self):
        """Panning and zooming is not an edit either — same reason as `position`."""
        original = _inner()
        panned = _inner()
        panned.viewport.x += 500
        panned.viewport.zoom = 2.0

        assert definition_digest(panned) == definition_digest(original)

    def test_the_digest_is_stable_across_repeated_calls(self):
        """A digest that drifts between calls would make every pin go stale.

        The serialiser sorts keys for this reason; this holds that in place.
        """
        graph = _inner()
        assert definition_digest(graph) == definition_digest(graph)
        # And a round trip through JSON, which is how it travels.
        again = WorkflowGraph.model_validate(graph.model_dump(mode="json"))
        assert definition_digest(again) == definition_digest(graph)
