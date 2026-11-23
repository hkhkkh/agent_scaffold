"""Tidy layout: the positions generation and the canvas button share.

The picture is the product here. A graph whose nodes are technically correct
but parked tens of thousands of pixels below the viewport reads to the user as
an empty canvas, so these tests pin the geometry: columns by dependency depth,
start in the first column, and a stable result for the same graph.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.graph.layout import layout_graph
from app.graph.schema import WorkflowGraph
from app.main import app


def _node(node_id: str, node_type: str, x: float = 0, y: float = 0) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "label": node_id,
        "position": {"x": x, "y": y},
    }


def _graph(nodes: list[dict], edges: list[dict]) -> WorkflowGraph:
    return WorkflowGraph.model_validate(
        {"id": "layout-test", "name": "layout", "nodes": nodes, "edges": edges}
    )


def _edge(source: str, target: str) -> dict:
    return {
        "id": f"{source}-{target}",
        "source": source,
        "target": target,
        "sourceHandle": "out",
        "targetHandle": "in",
    }


class TestLayering:
    def test_depth_decides_the_column(self):
        graph = _graph(
            [
                _node("s", "start"),
                _node("a", "prompt"),
                _node("b", "llm"),
                _node("c", "end"),
            ],
            [_edge("s", "a"), _edge("a", "b"), _edge("b", "c")],
        )

        laid_out = layout_graph(graph)

        assert [laid_out.node(node_id).position.x for node_id in "sabc"] == [
            0,
            340,
            680,
            1020,
        ]

    def test_parallel_branches_share_a_column_and_stack(self):
        graph = _graph(
            [
                _node("s", "start"),
                _node("a", "prompt"),
                _node("b", "prompt"),
                _node("end", "end"),
            ],
            [
                _edge("s", "a"),
                _edge("s", "b"),
                _edge("a", "end"),
                _edge("b", "end"),
            ],
        )

        laid_out = layout_graph(graph)

        a, b = laid_out.node("a").position, laid_out.node("b").position
        assert (a.x, a.y) == (340, 0)
        assert (b.x, b.y) == (340, 180)
        assert laid_out.node("end").position.x == 680

    def test_start_is_always_in_the_first_column(self):
        """A Start node with a predecessor still defines the left edge."""
        graph = _graph(
            [
                _node("s", "start"),
                _node("loop", "prompt"),
            ],
            [_edge("loop", "s"), _edge("s", "loop")],
        )

        laid_out = layout_graph(graph)

        assert laid_out.node("s").position.x == 0

    def test_the_original_graph_is_not_mutated(self):
        graph = _graph(
            [_node("s", "start", 10, 20), _node("e", "end", 900, 900)],
            [_edge("s", "e")],
        )

        laid_out = layout_graph(graph)

        assert graph.node("s").position.x == 10
        assert laid_out.node("s").position.x == 0
        assert laid_out.node("e").position.x == 340

    def test_a_long_chain_wraps_into_bands(self):
        """Twelve levels must not become one 4,000-pixel-wide line."""
        nodes = [_node("n0", "start")]
        nodes += [_node(f"n{index}", "prompt") for index in range(1, 13)]
        edges = [_edge(f"n{index}", f"n{index + 1}") for index in range(12)]
        graph = _graph(nodes, edges)

        laid_out = layout_graph(graph)

        xs = [node.position.x for node in laid_out.nodes]
        ys = [node.position.y for node in laid_out.nodes]
        assert max(xs) <= 5 * 340
        # The second band starts below the first, so a wrapped chain is not one
        # row of twelve columns.
        assert max(ys) >= 260

    def test_it_is_deterministic(self):
        graph = _graph(
            [_node("s", "start"), _node("a", "prompt"), _node("e", "end")],
            [_edge("s", "a"), _edge("a", "e")],
        )

        first = [(n.id, n.position.x, n.position.y) for n in layout_graph(graph).nodes]
        second = [(n.id, n.position.x, n.position.y) for n in layout_graph(graph).nodes]

        assert first == second

    def test_an_empty_graph_is_left_alone(self):
        graph = _graph([], [])

        assert layout_graph(graph).nodes == []

    def test_a_cycle_does_not_recurse_forever(self):
        graph = _graph(
            [_node("a", "prompt"), _node("b", "prompt")],
            [_edge("a", "b"), _edge("b", "a")],
        )

        laid_out = layout_graph(graph)

        assert all(node.position.x >= 0 for node in laid_out.nodes)


def test_the_endpoint_returns_a_tidy_graph():
    payload = _graph(
        [_node("s", "start", 0, 22000), _node("e", "end", 3674, 29425)],
        [_edge("s", "e")],
    ).model_dump(mode="json")

    with TestClient(app, client=("127.0.0.1", 55555)) as client:
        response = client.post("/api/workflows/layout", json=payload)

    assert response.status_code == 200
    graph = response.json()["graph"]
    positions = {node["id"]: node["position"] for node in graph["nodes"]}
    assert positions["s"]["x"] == 0
    assert positions["e"]["x"] == 340
    assert positions["s"]["y"] == 0


def test_expanded_layout_is_one_straight_line():
    """per_band=0 is the user's escape hatch from folded bands."""
    nodes = [_node("n0", "start")]
    nodes += [_node(f"n{index}", "prompt") for index in range(1, 12)]
    edges = [_edge(f"n{index}", f"n{index + 1}") for index in range(11)]
    graph = _graph(nodes, edges)

    laid_out = layout_graph(graph, per_band=0)

    positions = [laid_out.node(f"n{index}").position for index in range(12)]
    assert [position.x for position in positions] == [index * 340 for index in range(12)]
    assert {position.y for position in positions} == {0}


def test_the_endpoint_accepts_the_expanded_mode():
    nodes = [_node("n0", "start")]
    nodes += [_node(f"n{index}", "prompt") for index in range(1, 12)]
    edges = [_edge(f"n{index}", f"n{index + 1}") for index in range(11)]
    payload = _graph(nodes, edges).model_dump(mode="json")

    with TestClient(app, client=("127.0.0.1", 55556)) as client:
        response = client.post("/api/workflows/layout?per_band=0", json=payload)

    assert response.status_code == 200
    graph = response.json()["graph"]
    positions = {node["id"]: node["position"] for node in graph["nodes"]}
    assert positions["n11"]["x"] == 11 * 340
    assert positions["n11"]["y"] == 0


def test_expanded_layout_with_unconnected_nodes_is_still_a_horizontal_line():
    """No edges means every depth is 0; the line mode must not stack a column."""
    graph = _graph(
        [_node("a", "start"), _node("b", "prompt"), _node("c", "end")],
        [],
    )

    laid_out = layout_graph(graph, per_band=0)

    positions = [laid_out.node(node_id).position for node_id in ("a", "b", "c")]
    assert [position.x for position in positions] == [0, 340, 680]
    assert {position.y for position in positions} == {0}
