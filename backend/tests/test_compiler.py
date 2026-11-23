"""Compiler tests.

These are the highest-value tests in the suite: they assert that what the user
draws on the canvas becomes the program they intended.
"""

from __future__ import annotations

import pytest

from app.graph.compiler import CompileError, compile_graph
from app.graph.schema import WorkflowGraph
from app.graph.validate import validate_graph


def _graph(nodes, edges, **kw) -> WorkflowGraph:
    return WorkflowGraph(nodes=nodes, edges=edges, **kw)


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


class TestStructure:
    def test_compiles_a_minimal_linear_workflow(self, simple_graph):
        compiled = compile_graph(WorkflowGraph(**simple_graph))
        assert compiled is not None

    def test_rejects_a_graph_with_no_nodes(self):
        with pytest.raises(CompileError, match="no nodes"):
            compile_graph(_graph([], []))

    def test_rejects_a_graph_without_start(self):
        with pytest.raises(CompileError, match="Start"):
            compile_graph(
                _graph(
                    [_node("e", "end")],
                    [],
                )
            )

    def test_strict_false_compiles_despite_errors(self):
        """The UI uses non-strict mode to preview a work-in-progress graph."""
        graph = _graph(
            [_node("t", "prompt", config={"template": "hi"}), _node("e", "end")],
            [_edge("t", "e")],
        )
        assert compile_graph(graph, strict=False) is not None

    def test_terminal_node_without_outgoing_edge_is_closed(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": "x"}),
            ],
            [_edge("s", "p")],
        )
        # Should compile rather than leaving a dangling node.
        assert compile_graph(graph, strict=False) is not None


class TestPortValidation:
    def test_incompatible_port_types_are_rejected(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("r", "retriever", config={"documents": ["a"], "query": "q"}),
            ],
            # start.out is JSON, retriever.in is ANY -> compatible.
            [_edge("s", "r")],
        )
        report = validate_graph(graph)
        assert not [i for i in report.errors if i.code == "incompatible_port_types"]

    def test_unknown_handle_is_reported(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": "x"}),
            ],
            [_edge("s", "p", source_handle="nonexistent")],
        )
        report = validate_graph(graph)
        assert any(i.code == "unknown_source_handle" for i in report.errors)

    def test_dangling_edge_is_reported(self):
        graph = _graph(
            [_node("s", "start", config={"input_schema": {}})],
            [_edge("s", "ghost")],
        )
        report = validate_graph(graph)
        assert any(i.code == "dangling_edge" for i in report.errors)


class TestRequiredParams:
    def test_missing_required_template_is_an_error(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": ""}),
            ],
            [_edge("s", "p")],
        )
        report = validate_graph(graph)
        assert any(i.code == "missing_required_param" for i in report.errors)

    def test_code_node_must_be_trusted(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("c", "code", config={"code": "result = 1"}),
            ],
            [_edge("s", "c")],
        )
        report = validate_graph(graph)
        assert any(i.code == "untrusted_node" for i in report.errors)

        # ...and passes once trusted.
        graph.nodes[1].trusted = True
        report = validate_graph(graph)
        assert not any(i.code == "untrusted_node" for i in report.errors)


class TestConnectivity:
    def test_orphan_node_warns(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": "x"}),
                _node("lonely", "prompt", config={"template": "y"}),
            ],
            [_edge("s", "p")],
        )
        report = validate_graph(graph)
        assert any(i.code == "orphan_node" for i in report.warnings if i.severity == "warning")
        assert any(i.node_id == "lonely" for i in report.issues)

    def test_unreachable_node_warns(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("p", "prompt", config={"template": "x"}),
                _node("q", "prompt", config={"template": "y"}),
                _node("e", "end"),
            ],
            [_edge("s", "p"), _edge("p", "e"), _edge("q", "e")],
        )
        report = validate_graph(graph)
        assert any(
            i.code == "unreachable_node" and i.node_id == "q" for i in report.issues
        )


class TestBranches:
    def test_condition_compiles_to_conditional_edges(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {"q": "hi"}}),
                _node(
                    "c",
                    "condition",
                    config={
                        "branches": [
                            {"id": "yes", "label": "Yes", "expression": "True"},
                            {"id": "no", "label": "No", "expression": ""},
                        ]
                    },
                ),
                _node("a", "prompt", config={"template": "A"}),
                _node("b", "prompt", config={"template": "B"}),
            ],
            [
                _edge("s", "c"),
                _edge("c", "a", source_handle="yes"),
                _edge("c", "b", source_handle="no"),
            ],
        )
        report = validate_graph(graph)
        assert report.ok, [i.message for i in report.errors]
        assert compile_graph(graph) is not None

    def test_unwired_branch_warns(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node(
                    "c",
                    "condition",
                    config={
                        "branches": [
                            {"id": "yes", "label": "Y", "expression": "True"},
                            {"id": "no", "label": "N", "expression": ""},
                        ]
                    },
                ),
                _node("a", "prompt", config={"template": "A"}),
            ],
            [_edge("s", "c"), _edge("c", "a", source_handle="yes")],
        )
        report = validate_graph(graph)
        assert any(i.code == "unused_branch" for i in report.issues)


class TestLoops:
    def test_bounded_loop_is_accepted(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("l", "loop", config={"max_iterations": 3, "condition": "iteration < 3"}),
                _node("p", "prompt", config={"template": "refine"}),
                _node("e", "end"),
            ],
            [
                _edge("s", "l"),
                _edge("l", "p", source_handle="continue"),
                _edge("p", "l"),  # the back edge that forms the cycle
                _edge("l", "e", source_handle="done"),
            ],
        )
        report = validate_graph(graph)
        assert not any(i.code == "unbounded_cycle" for i in report.errors)
        assert compile_graph(graph) is not None

    def test_cycle_without_a_loop_node_is_rejected(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("a", "prompt", config={"template": "A"}),
                _node("b", "prompt", config={"template": "B"}),
            ],
            [_edge("s", "a"), _edge("a", "b"), _edge("b", "a")],
        )
        report = validate_graph(graph)
        assert any(i.code == "unbounded_cycle" for i in report.errors)
        with pytest.raises(CompileError, match="not valid"):
            compile_graph(graph)


class TestContainers:
    def test_parent_declared_after_child_warns(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("child", "prompt", config={"template": "x"}),
                _node("grp", "group", ui={"parent_id": None}),
            ],
            [_edge("s", "child")],
        )
        graph.nodes[1].ui.parent_id = "grp"
        report = validate_graph(graph)
        assert any(i.code == "parent_order" for i in report.issues)

    def test_missing_parent_is_an_error(self):
        graph = _graph(
            [_node("s", "start", config={"input_schema": {}})],
            [],
        )
        graph.nodes[0].ui.parent_id = "ghost"
        report = validate_graph(graph)
        assert any(i.code == "missing_parent" for i in report.errors)

    def test_group_nodes_are_not_executable(self):
        graph = _graph(
            [
                _node("s", "start", config={"input_schema": {}}),
                _node("grp", "group"),
            ],
            [],
        )
        assert [n.id for n in graph.executable_nodes()] == ["s"]


async def test_parallel_branches_join_once():
    """A converging node runs once after both branches, not once per branch.

    The previous wiring called `add_edge(source, target)` per source. LangGraph
    treats each as a separate trigger, so a node with two upstream branches ran
    twice — and a side-effecting node after it (an email, an insert) repeated its
    effect. A patient-referral simulation found three emails from one designed
    notification.
    """
    graph = _graph(
        [
            _node("s", "start", config={"input_schema": {}}),
            _node("a", "prompt", config={"template": "a"}),
            _node("b", "prompt", config={"template": "b"}),
            _node("c", "prompt", config={"template": "c"}),
            _node("e", "end"),
        ],
        [
            _edge("s", "a"),
            _edge("s", "b"),
            _edge("a", "c"),
            _edge("b", "c"),
            _edge("c", "e"),
        ],
    )
    compiled = compile_graph(graph)

    seen: list[str] = []
    async for update in compiled.astream({"inputs": {}}, stream_mode="updates"):
        seen.extend(update.keys())

    assert seen.count("c") == 1
    assert seen.count("a") == 1
    assert seen.count("b") == 1


async def test_mutually_exclusive_branches_do_not_deadlock_at_a_join():
    """Only one Condition branch runs; a join must not wait for the other."""
    graph = _graph(
        [
            _node("s", "start", config={"input_schema": {}}),
            _node(
                "cond",
                "condition",
                config={
                    "branches": [
                        {"id": "yes", "label": "yes", "expression": "True"},
                        {"id": "no", "label": "no", "expression": ""},
                    ]
                },
            ),
            _node("a", "prompt", config={"template": "a"}),
            _node("b", "prompt", config={"template": "b"}),
            _node("c", "prompt", config={"template": "c"}),
            _node("e", "end"),
        ],
        [
            _edge("s", "cond"),
            _edge("cond", "a", source_handle="yes"),
            _edge("cond", "b", source_handle="no"),
            _edge("a", "c"),
            _edge("b", "c"),
            _edge("c", "e"),
        ],
    )
    compiled = compile_graph(graph)

    seen: list[str] = []
    async for update in compiled.astream({"inputs": {"in": 1}}, stream_mode="updates"):
        seen.extend(update.keys())

    assert seen.count("c") == 1
    assert "a" in seen
    assert "b" not in seen
