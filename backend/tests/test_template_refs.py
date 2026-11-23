"""Template-reference validation.

A `{{ vars.<node>... }}` path that names a missing node or a missing output
handle does not fail at compile time — the renderer turns it into an empty
string. These tests pin the rule that makes it a validation error instead,
because the generator's self-correction loop only reacts to the report.
"""

from __future__ import annotations

from app.graph.schema import WorkflowGraph
from app.graph.validate import validate_graph


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


def _issues(nodes, edges):
    report = validate_graph(WorkflowGraph(nodes=nodes, edges=edges))
    return {issue.code for issue in report.issues}


def _valid_tool_prompt_graph(template: str):
    return (
        [
            _node("s", "start", config={"input_schema": {}}),
            _node(
                "search",
                "tool",
                config={"tool": "web_search", "arguments": {"query": "x"}},
            ),
            _node("p", "prompt", config={"template": template}),
            _node("e", "end"),
        ],
        [_edge("s", "search"), _edge("search", "p"), _edge("p", "e")],
    )


def test_valid_paths_are_accepted():
    codes = _issues(*_valid_tool_prompt_graph("{{ vars.search.out.results.0.url }}"))
    assert not {code for code in codes if code.startswith("unknown_template")}
    assert "loop_iteration_misplaced" not in codes


def test_unknown_node_is_an_error():
    codes = _issues(*_valid_tool_prompt_graph("{{ vars.missing.out }}"))
    assert "unknown_template_node" in codes


def test_unknown_handle_is_an_error():
    codes = _issues(*_valid_tool_prompt_graph("{{ vars.search.nope }}"))
    assert "unknown_template_handle" in codes


def test_tool_argument_json_is_scanned():
    nodes = [
        _node("s", "start", config={"input_schema": {}}),
        _node(
            "search",
            "tool",
            config={"tool": "web_search", "arguments": {"query": "{{ vars.missing.out }}"}},
        ),
        _node("e", "end"),
    ]
    codes = _issues(nodes, [_edge("s", "search"), _edge("search", "e")])
    assert "unknown_template_node" in codes


def test_code_fields_are_not_treated_as_templates():
    """A Code node may legitimately contain `{{` while building a prompt."""
    nodes = [
        _node("s", "start", config={"input_schema": {}}),
        _node("c", "code", config={"code": "x = '{{ vars.missing.out }}'"}),
        _node("e", "end"),
    ]
    codes = _issues(nodes, [_edge("s", "c"), _edge("c", "e")])
    assert "unknown_template_node" not in codes


def _loop_graph(template: str):
    return (
        [
            _node("s", "start", config={"input_schema": {}}),
            _node("gate", "loop", config={"max_iterations": 3, "condition": "iteration <= 2"}),
            _node("p", "prompt", config={"template": template}),
            _node("e", "end"),
        ],
        [
            _edge("s", "gate"),
            _edge("gate", "p", "continue"),
            _edge("p", "gate"),
            _edge("gate", "e", "done"),
        ],
    )


def test_loop_iteration_top_level_is_accepted():
    codes = _issues(*_loop_graph("round {{ vars.gate.iteration }}"))
    assert "loop_iteration_misplaced" not in codes
    assert "unknown_template_field" not in codes


def test_loop_iteration_nested_under_out_is_rejected():
    codes = _issues(*_loop_graph("round {{ vars.gate.out.iteration }}"))
    assert "loop_iteration_misplaced" in codes


def test_loop_unknown_top_level_field_is_rejected():
    codes = _issues(*_loop_graph("{{ vars.gate.nope }}"))
    assert "unknown_template_field" in codes
