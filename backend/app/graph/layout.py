"""Tidy node placement for generated and hand-drawn workflows.

A model has no idea about screen coordinates, so generation computes them here
rather than asking. Layering by longest-path depth puts each node one column
right of its deepest predecessor, which is what a user would have drawn by hand.

Kept in the graph layer, not the AI layer, because the canvas offers the same
"tidy layout" action for a graph the user already has. Both paths must produce
the same picture, so both call this one function.
"""

from __future__ import annotations

from app.graph.schema import EdgeSpec, NodeSpec, Position, WorkflowGraph


def layout_nodes(
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
    *,
    per_band: int = 6,
) -> None:
    """Assign tidy positions in place, left to right by dependency depth.

    ``per_band`` is how many dependency columns sit side by side before the
    layout wraps into a new band. ``0`` means "never wrap": one long left-to-right
    line. Wrapping is the readable default for a 20-node graph, but it trades
    horizontal travel for vertical bands, which hides some connections behind
    nodes; the canvas therefore offers both and lets the user choose.
    """
    if not nodes:
        return

    by_id = {node.id: node for node in nodes}
    incoming: dict[str, list[str]] = {node.id: [] for node in nodes}
    for edge in edges:
        if edge.source in incoming and edge.target in incoming:
            incoming[edge.target].append(edge.source)

    depth: dict[str, int] = {}

    def resolve(node_id: str, stack: set[str]) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in stack:
            # A cycle (a Loop node). Break it: the loop body sits one column
            # right of the loop, which reads correctly on screen.
            return 0
        stack = stack | {node_id}
        parents = incoming.get(node_id) or []
        level = 0 if not parents else max(resolve(p, stack) for p in parents) + 1
        depth[node_id] = level
        return level

    for node in nodes:
        resolve(node.id, set())

    # A Start node always belongs in the first column, whatever the depths say.
    start_ids = [node.id for node in nodes if node.type == "start"]
    if start_ids:
        offset = min(depth.get(sid, 0) for sid in start_ids)
        # A cycle broken at the Start node can give a back-edge node a depth
        # below it; clamp so nothing is laid out to the left of Start.
        depth = {key: max(0, value - offset) for key, value in depth.items()}

    if per_band <= 0:
        # One readable line, not one column with every node stacked: without
        # edges all depths are 0, and the banding loop below would put them in
        # the same column. Dependency depth decides the order; declaration
        # order breaks ties inside a depth.
        order = {node.id: index for index, node in enumerate(nodes)}
        ordered = sorted(nodes, key=lambda node: (depth.get(node.id, 0), order[node.id]))
        for index, node in enumerate(ordered):
            node.position = Position(x=index * 340, y=0)
        return

    columns: dict[int, list[str]] = {}
    for node in nodes:
        columns.setdefault(depth.get(node.id, 0), []).append(node.id)

    # A long chain would otherwise become one 4,000-pixel line that only fits
    # on screen at a zoom where the labels are unreadable. Wrap it into bands
    # of at most six columns, alternating direction so the edge from one band
    # to the next is a short vertical drop at the edge rather than a line all
    # the way back across the canvas.
    x_gap, y_gap = 340, 180
    band_gap = 260
    levels = sorted(columns)
    if per_band <= 0 or per_band > len(levels):
        per_band = max(1, len(levels))
    top = 0
    for band_number, start in enumerate(range(0, len(levels), per_band)):
        band = levels[start : start + per_band]
        rows = max(len(columns[level]) for level in band)
        left_to_right = band_number % 2 == 0
        for slot, level in enumerate(band):
            column = slot if left_to_right else per_band - 1 - slot
            for row, node_id in enumerate(columns[level]):
                by_id[node_id].position = Position(
                    x=column * x_gap, y=top + row * y_gap
                )
        top += rows * y_gap + band_gap


def layout_graph(graph: WorkflowGraph, *, per_band: int = 6) -> WorkflowGraph:
    """A copy of `graph` with tidy positions; the original is untouched."""
    laid_out = graph.model_copy(deep=True)
    layout_nodes(laid_out.nodes, laid_out.edges, per_band=per_band)
    return laid_out
