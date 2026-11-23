"""A readable description of a graph, for a model that cannot see the canvas.

Why this exists next to :mod:`app.graph.validate`
-------------------------------------------------

``validate_graph`` answers *"would this run?"* — it is a gate, and everything it
reports is something that would stop the run. That is the right answer for the
canvas's error badges and for the repair loop, and it is the wrong answer for an
author who is still building.

An authoring model needs the other question: *"what have I built, and does it
have the shape I meant?"* A graph can pass validation completely — every node
connected, one entry, one exit — and still be nothing like the intent: a body
that is a chain where it should be three parallel branches, a condition whose
"skip" branch feeds data into the merge, an End node wired to a side effect.
None of those are errors. All of them are visible in the wiring, and only in the
wiring.

So this module renders the wiring. Three things it deliberately does:

**It is text, not JSON.** ``WorkflowGraph.model_dump()`` is already available to
the model and already useless at thirty nodes — a wall of coordinates, colours
and empty configs, in which the one line that matters (this branch goes nowhere)
is indistinguishable from the ninety that do not. The rendering below drops
everything the author cannot act on.

**It is deterministic.** Same graph, same string. A model that re-reads the
canvas after every edit would otherwise have to diff two unrelated orderings to
notice what changed, which it will not do.

**It reports what validation does not.** Unreachable nodes, dead ends, declared
but unwired branches and untrusted Code nodes are all *legal* graphs. They are
also the four mistakes an author most often makes and least often notices.

Pure and side-effect free: no I/O, no registry lookups beyond the node specs, so
it is testable without an LLM and callable from a tool, an endpoint or a test.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.graph.schema import NodeSpec, WorkflowGraph
from app.graph.validate import validate_graph
from app.nodes import effective_outputs, has_node_type

__all__ = ["describe_graph"]


def describe_graph(graph: WorkflowGraph, *, max_nodes: int = 200) -> str:
    """Render ``graph`` as a flow listing plus the problems worth acting on.

    ``max_nodes`` caps the listing so a pathological graph cannot blow up a
    prompt. The counts and the problems are still complete — truncating those
    would hide exactly the information the caller asked for.
    """
    if not graph.nodes:
        return "CANVAS — empty. Nothing has been added yet."

    order, back_edges = _flow_order(graph)
    sections = [
        _header(graph),
        _flow(graph, order, back_edges, max_nodes),
        _problems(graph),
        _structure(graph),
    ]
    return "\n\n".join(section for section in sections if section)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(graph: WorkflowGraph) -> str:
    counts = Counter(node.type for node in graph.nodes)
    mix = " · ".join(
        f"{count} {node_type}" if count > 1 else node_type
        for node_type, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return (
        f"CANVAS — {len(graph.nodes)} nodes, {len(graph.edges)} edges\n  {mix}"
    )


def _flow(
    graph: WorkflowGraph,
    order: list[str],
    back_edges: set[tuple[str, str]],
    max_nodes: int,
) -> str:
    """One line per node, in flow order, with where each edge goes.

    The edge list is the point of the section: a reader scanning the right-hand
    column sees an unwired branch or a missing back edge immediately, which is
    the failure this section exists to make visible.
    """
    by_id = {node.id: node for node in graph.nodes}
    outgoing: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if edge.source in outgoing:
            handle = edge.source_handle or "out"
            marker = " (loops back)" if (edge.source, edge.target) in back_edges else ""
            # The label says *why* an edge exists; printing only ids makes a
            # dense graph a tracing exercise for the model on its next round.
            purpose = f" [{edge.label}]" if edge.label else ""
            outgoing[edge.source].append(f"{handle}→ {edge.target}{purpose}{marker}")

    lines = ["FLOW (in dependency order; the Loop body repeats)"]
    depth = _depths(graph, order, back_edges)

    for node_id in order[:max_nodes]:
        node = by_id[node_id]
        targets = " | ".join(outgoing.get(node_id) or []) or "(nothing follows)"
        label = (node.label or node.id)[:28]
        lines.append(
            f"  [{depth.get(node_id, 0):>2}] {node_id:<8} {node.type:<11} "
            f"{label:<28} {targets}"
        )

    if len(order) > max_nodes:
        lines.append(f"  ... and {len(order) - max_nodes} more node(s), not listed.")

    orphans = [n.id for n in graph.nodes if n.id not in set(order)]
    if orphans:
        lines.append(
            "  unreachable (no path from Start): " + ", ".join(sorted(orphans)[:20])
        )

    return "\n".join(lines)


def _problems(graph: WorkflowGraph) -> str:
    """What ``validate_graph`` says, split by severity so errors read first."""
    report = validate_graph(graph)
    if not report.issues:
        return "PROBLEMS — none. The graph would compile and run."

    lines = ["PROBLEMS"]
    for issue in report.errors:
        where = f" [{issue.node_id}]" if issue.node_id else ""
        lines.append(f"  ERROR  {issue.code}{where}: {issue.message}")
    for issue in report.warnings:
        where = f" [{issue.node_id}]" if issue.node_id else ""
        lines.append(f"  warn   {issue.code}{where}: {issue.message}")
    return "\n".join(lines)


def _structure(graph: WorkflowGraph) -> str:
    """The legal-but-wrong shapes. None of these fail validation.

    Reported separately from :func:`_problems` because the fix is different in
    kind: those are "the run will break", these are "the run will not do what
    you meant", and a model told only about the first will hand back a graph
    that runs perfectly and answers the wrong question.
    """
    notes: list[str] = []

    dead_ends = [
        node.id
        for node in graph.nodes
        if node.type != "end" and not graph.outgoing(node.id)
    ]
    if dead_ends:
        notes.append(
            "dead ends — nothing follows these, and they are not End nodes: "
            + ", ".join(sorted(dead_ends))
        )

    for node in graph.nodes:
        if not has_node_type(node.type):
            continue
        outgoing = graph.outgoing(node.id)

        if node.type == "condition":
            wired = {edge.source_handle for edge in outgoing}
            declared = {port.id for port in effective_outputs(node)}
            missing = declared - wired
            if missing:
                notes.append(
                    f"condition '{node.id}' has branch(es) wired to nothing: "
                    + ", ".join(sorted(missing))
                    + " — an unwired branch is a dead end for any input that "
                    "matches it"
                )
            for handle in {edge.source_handle for edge in outgoing}:
                count = sum(1 for e in outgoing if e.source_handle == handle)
                if count > 1:
                    notes.append(
                        f"condition '{node.id}' has {count} edges on branch "
                        f"'{handle}' — only the last is kept; the rest are "
                        "silently dropped"
                    )

        if node.type == "loop":
            wired = {edge.source_handle for edge in outgoing}
            if "continue" not in wired:
                notes.append(
                    f"loop '{node.id}' has no 'continue' target, so its body "
                    "never repeats — this is a branch, not a loop"
                )
            if "done" not in wired:
                notes.append(
                    f"loop '{node.id}' has no 'done' target; the run stops here "
                    "when the loop ends"
                )

        if node.type == "end":
            incoming = graph.incoming(node.id)
            if not incoming:
                notes.append(f"end node '{node.id}' has nothing wired into it")
            for edge in incoming:
                source = graph.node(edge.source)
                if source is not None and source.type == "memory":
                    notes.append(
                        f"end node '{node.id}' is wired to memory node "
                        f"'{source.id}', which returns an acknowledgement "
                        "rather than stored text — the run result will be a key, "
                        "not the content"
                    )

        if node.type == "agent" and not (node.config or {}).get("tools"):
            notes.append(
                f"agent '{node.id}' has no 'tools' list, so every registered "
                "tool is available to it — name the two or three it needs"
            )

        if node.type == "tool" and not (node.config or {}).get("tool"):
            notes.append(f"tool node '{node.id}' has no tool selected")

    untrusted = [
        node.id for node in graph.nodes if node.type == "code" and not node.trusted
    ]
    if untrusted:
        notes.append(
            "code nodes awaiting the user's Trusted tick (they will not run "
            "until then): " + ", ".join(sorted(untrusted))
        )

    if not notes:
        return "STRUCTURE — no dead ends, no unwired branches, nothing suspicious."
    return "STRUCTURE (legal, but probably not what you meant)\n" + "\n".join(
        f"  - {note}" for note in notes
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def _flow_order(graph: WorkflowGraph) -> tuple[list[str], set[tuple[str, str]]]:
    """Node ids in dependency order, plus the edges that close a cycle.

    A depth-first walk from the Start node, following edges in declaration
    order, emitting a node the first time it is reached and recursing after.
    Cycles terminate because a node already on the current path is not
    re-entered — which is what makes the output stable rather than dependent on
    where the cycle happened to be cut.

    The back edges are returned rather than merely skipped, because they are
    also what :func:`_depths` has to ignore. Leaving them in makes the Loop gate
    the *deepest* node in the graph — its longest path runs all the way around
    the body it starts — so the one node a reader looks for first appears at the
    bottom of the listing under a depth of 24. Measured on a 42-node graph, and
    the reason this function returns two things.

    Nodes unreachable from Start are appended in id order, so they are visible
    rather than missing. :func:`_flow` calls them out by name as well.
    """
    by_id = {node.id: node for node in graph.nodes}
    outgoing: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if edge.source in outgoing and edge.target in by_id:
            outgoing[edge.source].append(edge.target)

    starts = [n.id for n in graph.nodes if n.type == "start"]
    roots = starts or [n.id for n in graph.nodes if not graph.incoming(n.id)]
    roots = roots or ([graph.nodes[0].id] if graph.nodes else [])

    seen: set[str] = set()
    order: list[str] = []
    back_edges: set[tuple[str, str]] = set()

    def walk(node_id: str, path: frozenset[str]) -> None:
        if node_id in seen or node_id in path:
            return
        seen.add(node_id)
        order.append(node_id)
        for target in outgoing.get(node_id, []):
            if target in path:
                back_edges.add((node_id, target))
            walk(target, path | {node_id})

    for root in roots:
        walk(root, frozenset())

    for node in graph.nodes:
        if node.id not in seen:
            order.append(node.id)

    return order, back_edges


def _depths(
    graph: WorkflowGraph,
    order: list[str],
    back_edges: set[tuple[str, str]],
) -> dict[str, int]:
    """Longest-path depth per node, for the left-hand column of the flow list.

    Longest path rather than shortest, so a node that merges two branches sits
    at the depth of the deeper one — which is where a reader expects to find it
    and where the canvas lays it out.

    Back edges are excluded, so the walk stays a DAG. Without that, a loop's
    gate is pushed to the far end of the graph by the body it precedes.
    """
    incoming: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if edge.source in incoming and edge.target in incoming:
            if (edge.source, edge.target) in back_edges:
                continue
            incoming[edge.target].append(edge.source)

    depth: dict[str, int] = {}

    def resolve(node_id: str) -> int:
        if node_id in depth:
            return depth[node_id]
        # Guarded anyway: an edge the DFS never classified as a back edge can
        # still appear here, and recursing forever is a worse failure than a
        # slightly generous depth.
        depth[node_id] = 0
        parents = incoming.get(node_id) or []
        level = 0 if not parents else max(resolve(p) for p in parents) + 1
        depth[node_id] = level
        return level

    for node_id in order:
        resolve(node_id)

    return depth
