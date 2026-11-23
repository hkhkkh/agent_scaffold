"""Sub-workflow expansion: a saved workflow used as a node inside another one.

What this gives you
-------------------
A graph can reference another saved graph and be treated as one reusable unit.
Without it, "reuse a piece of a flow" degrades into copy-paste: either redraw the
same sub-flow in every agent, or grow one 200-node graph nobody can maintain.

How it works: **expand, then compile normally**
-----------------------------------------------
A ``subgraph`` node is replaced — before compilation — by the referenced graph's
contents, with every node id prefixed by the referencing node's id. The result is
an ordinary flat graph, so nothing downstream has to know subgraphs exist: the
event timeline, the checkpointer, conditional edges, tweaks, and validation all
keep working with no changes.

The one thing that is *not* a rewrite: the node doing the referencing stays, and
acts as the entry adapter. That falls out of how this compiler resolves inputs —
a node's inputs come from ``vars[<source node id>]`` — so:

    parent -> [h] -> h/first -> ... -> h/last -> parent's next node

``h`` receives the parent's wiring and passes it through, so ``h/first`` reads its
input from ``vars[h]`` exactly as it read ``vars[start]`` inside its own graph. At
the other end, the referenced graph's ``end`` node is dropped and the parent's
outgoing edges are re-sourced from whatever fed that ``end``. No adapter node type
is needed, because the edge handles already carry the mapping.

Two consequences worth knowing
------------------------------
1. **The step budget is shared.** After expansion there is one graph, so one
   ``recursion_limit``; a loop inside a sub-workflow consumes the parent's steps.
   Independent budgets would require nesting a compiled graph instead, which costs
   interrupts, events, and checkpoints — a bad trade for a budget number.
2. **The referenced graph's input declarations and output label are not used.**
   Inputs come from the parent's wiring, and the result is whatever the sub-flow's
   last node produced. Both are consequences of inlining, and both are visible in
   the timeline.

Why the provider is injected
----------------------------
Definitions live in the platform database, and this module must stay importable
without it: ``app.graph.compiler`` is imported by ``app.runtime.manager``, and the
runtime layer is not allowed to reach the platform's database (task 2.6 — an
exported agent has no such database). So the platform registers a loader, exactly
like the other capability seams, and a deployment without one refuses subgraph
nodes with a readable message instead of importing a database that is not there.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from app.graph.schema import EdgeSpec, NodeSpec, WorkflowGraph

#: Node type that references another workflow.
SUBGRAPH_TYPE = "subgraph"

#: Config key holding the referenced workflow id.
REF_KEY = "ref"

#: Config key holding the digest of the referenced definition at save time.
REF_HASH_KEY = "ref_hash"

#: Config key on a sub-flow's exit node: also record this node's output under
#: another node id.
#:
#: The parent's wiring is re-sourced from the exit nodes (so timing is right — an
#: edge from the referencing node would fire before the sub-flow ran), but a
#: user-authored template reads ``vars.<node id>`` **by name**. Without this, the
#: two ways of getting the sub-flow's result disagree: the edge gives the real
#: result while ``{{ vars.h.out }}`` gives the adapter's pass-through of the
#: input. Both must mean the same thing, and the one a user would write is the
#: result.
RESULT_FOR_KEY = "__result_for__"

#: Config key marking a reference as already inlined.
#:
#: The referencing node is kept after expansion (it is the entry adapter), so
#: without a mark a second pass would inline the same sub-flow again and produce
#: duplicate node ids. Marked rather than inferred because "was this node already
#: expanded?" is not answerable from the node's shape — an expanded reference and
#: an unexpanded one differ only in the edges around them.
#:
#: Only ever set on the in-memory expanded copy; nothing writes it to the store.
EXPANDED_MARKER = "__expanded__"

#: Separator between a referencing node's id and a node id from inside it.
#: A slash because node ids are unconstrained (only *workflow* ids are), and
#: because `a/b/c` reads as a path, which is what nesting is.
NAMESPACE_SEPARATOR = "/"

#: Node types that are structural inside a referenced graph and disappear when it
#: is inlined. See the module docstring for why each one goes.
STRUCTURAL_TYPES = frozenset({"start", "end"})


class SubgraphError(RuntimeError):
    """Raised when a referenced workflow cannot be inlined."""


# ---------------------------------------------------------------------------
# Provider seam
# ---------------------------------------------------------------------------

_LOAD: Callable[[str], WorkflowGraph | None] | None = None
_LIST: Callable[[], list[dict[str, Any]]] | None = None


def set_subgraph_provider(
    load: Callable[[str], WorkflowGraph | None],
    list_all: Callable[[], list[dict[str, Any]]] | None = None,
) -> None:
    """Install the platform's way of reading workflow definitions.

    ``load`` returns ``None`` for an unknown id rather than raising, so "not
    found" stays a normal answer the validator can report with a good message.
    """
    global _LOAD, _LIST
    _LOAD = load
    _LIST = list_all


def clear_subgraph_provider() -> None:
    """Remove the provider. Tests call this so a leaked one cannot mask a bug."""
    global _LOAD, _LIST
    _LOAD = None
    _LIST = None


def has_subgraph_provider() -> bool:
    return _LOAD is not None


def load_subgraph(ref: str) -> WorkflowGraph | None:
    """The definition a reference points at, or ``None`` if there is no such one."""
    if _LOAD is None:
        return None
    return _LOAD(ref)


def list_subgraph_options() -> list[dict[str, Any]]:
    """Saved workflows as ``[{"value": id, "label": name}]`` for a dropdown.

    Empty when no provider is installed, which is the honest answer for a
    deployment that has no workflow database: the dropdown offers nothing rather
    than offering definitions that cannot be read.
    """
    if _LIST is None:
        return []
    return _LIST()


# ---------------------------------------------------------------------------
# Reference helpers
# ---------------------------------------------------------------------------


def reference_of(node: NodeSpec) -> str:
    """The workflow id a ``subgraph`` node points at (empty when unset)."""
    value = (node.config or {}).get(REF_KEY)
    return value.strip() if isinstance(value, str) else ""


def pinned_digest(node: NodeSpec) -> str:
    """The digest recorded when the *referencing* graph was last saved."""
    value = (node.config or {}).get(REF_HASH_KEY)
    return value.strip() if isinstance(value, str) else ""


#: Node fields that decide behaviour, in a fixed order. Listed rather than
#: inherited from the model so that adding a field to ``NodeSpec`` is a decision
#: about the hash, not a silent change to every pinned reference in the database.
#:
#: ``position``, ``ui`` and ``trusted`` are deliberately absent: none of them
#: changes what runs. ``position`` is the one that matters — see
#: :func:`definition_digest`. ``description`` is the user's own prose about the
#: step; it is not sent to a model as an instruction, so editing it is not an edit
#: to the *definition* either. (It is part of the AI-fill input, which is a
#: different question from what the graph executes.)
_DIGEST_NODE_FIELDS = ("id", "type", "label", "config")

#: Edge fields. ``type`` is included because a conditional edge with the same
#: endpoints does something different from a plain one, and ``label`` because it
#: carries the branch value the router matches on.
#:
#: The handles are included as ``source_handle`` / ``target_handle`` — the model's
#: own names. An earlier version of this list said ``sourceHandle`` (the wire name)
#: and silently hashed ``None`` for both, which would have made re-wiring an edge to
#: a different output port invisible to the digest.
_DIGEST_EDGE_FIELDS = ("id", "source", "source_handle", "target", "target_handle", "type", "label")


def definition_digest(graph: WorkflowGraph) -> str:
    """A stable content hash of everything that affects how a graph executes.

    Deliberately not a version number. A version would need a version store, a
    migration, and a way to say "which version did this parent mean" — for the
    one guarantee that matters here ("editing a sub-workflow must not silently
    change the behaviour of graphs that use it"), a content hash is enough and is
    immune to renumbering.

    **The payload is built by hand rather than by `model_dump`.** Two attempts to
    do it the short way both failed, in opposite directions, and the reasons are
    worth keeping:

    * ``model_dump(exclude={"position"})`` — a string name reaches only *top-level*
      fields, so node coordinates were hashed. Measured: moving every node by
      (120, 40) changed the digest from ``d64b3f1dc83e5251`` to ``862391183a99d810``.
      Dragging a node on the canvas is not an edit, so every graph referencing that
      sub-workflow would report "edited since you pinned it" and refuse to run.
    * ``model_dump(exclude={"nodes": {"__all__": {"position"}}})`` — ``__all__``
      excludes *elements of the list*, not fields inside them, so ``nodes`` vanished
      entirely and every graph hashed the same. Measured: adding a node left the
      digest at ``5fd786320e1ebda4``.
    * ``Field(exclude=True)`` on ``NodeSpec.position`` — correct for the hash, and
      it also removed the key from **every** dump, so the canvas lost all node
      coordinates. Measured: ``"position" in graph.model_dump(mode="json")["nodes"][0]``
      was ``False``.

    Spelling out the fields instead makes the boundary explicit: it cannot
    accidentally include something that changes without being an edit, and it
    cannot accidentally drop a whole collection. The cost is that a new
    behaviour-bearing field on ``NodeSpec`` must be added to
    :data:`_DIGEST_NODE_FIELDS` on purpose — which is the right way round, because
    the alternative is silently invalidating every pinned reference in the
    database.
    """
    payload = {
        "id": graph.id,
        "name": graph.name,
        "description": graph.description,
        "state_schema": graph.state_schema,
        "nodes": [
            {field: getattr(node, field, None) for field in _DIGEST_NODE_FIELDS}
            for node in graph.nodes
        ],
        "edges": [
            {field: getattr(edge, field, None) for field in _DIGEST_EDGE_FIELDS}
            for edge in graph.edges
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def digest_matches(node: NodeSpec, graph: WorkflowGraph) -> bool:
    """Has the referenced definition changed since the reference was pinned?"""
    pinned = pinned_digest(node)
    return not pinned or pinned == definition_digest(graph)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(node: NodeSpec) -> WorkflowGraph:
    """Load and check the definition a ``subgraph`` node refers to.

    Raises :class:`SubgraphError` for every way this can be unusable, because the
    caller is compiling and the alternative is a confusing failure deeper in.
    """
    ref = reference_of(node)
    if not ref:
        raise SubgraphError(
            f"Node '{node.id}' is a sub-flow but names no workflow. "
            f"Pick one on the node."
        )
    if _LOAD is None:
        raise SubgraphError(
            f"Node '{node.id}' refers to workflow '{ref}', but this deployment has "
            "no workflow store to read it from."
        )

    graph = _LOAD(ref)
    if graph is None:
        raise SubgraphError(
            f"Node '{node.id}' refers to workflow '{ref}', which does not exist. "
            "It may have been deleted or renamed."
        )

    pinned = pinned_digest(node)
    if pinned and pinned != definition_digest(graph):
        raise SubgraphError(
            f"Node '{node.id}' pins workflow '{ref}' at {pinned}, but that "
            "workflow has been edited since (it is now "
            f"{definition_digest(graph)}). Re-save this workflow to accept the new "
            "version, or pick a different one."
        )
    return graph


def chain_has_cycle(graph: WorkflowGraph, *, _seen: tuple[str, ...] = ()) -> list[str] | None:
    """The reference chain that closes a loop, or ``None`` if there is none.

    Walked explicitly rather than left to the recursive expansion, which would
    fail as a ``RecursionError`` — a stack trace that says nothing about which
    two workflows point at each other.
    """
    for node in graph.nodes:
        if node.type != SUBGRAPH_TYPE:
            continue
        ref = reference_of(node)
        if not ref:
            continue
        if ref in _seen or ref == graph.id:
            return [*_seen, graph.id, ref]
        child = load_subgraph(ref)
        if child is None:
            continue
        found = chain_has_cycle(child, _seen=(*_seen, graph.id))
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Rewriting the sub-flow's own node-id references
# ---------------------------------------------------------------------------

#: ``vars.<id>.`` inside a string config value. Word-boundary at both ends so
#: `vars.startx.out` is not mistaken for a reference to `start`.
_REFERENCE_PREFIX = "vars."


def _rewrite_refs(value: Any, mapping: dict[str, str]) -> Any:
    """Rewrite ``vars.<old id>.`` to ``vars.<new id>.`` throughout a config value.

    **Without this, a sub-flow silently reads the wrong thing.** Node configs
    address other nodes by id — ``{{ vars.start.out.q }}`` in a template, or a
    ``vars.step1.out.score`` in a Condition expression — and after inlining those
    ids are namespaced. Left alone, `vars.start` would resolve to the *parent's*
    Start node, producing a plausible value at run time and no error anywhere.

    Walks dicts and lists because config values nest (`arguments`, `fields`,
    `branches`), and only touches strings, so numbers and booleans pass through.
    """
    if isinstance(value, str):
        for old, new in mapping.items():
            value = value.replace(f"{_REFERENCE_PREFIX}{old}.", f"{_REFERENCE_PREFIX}{new}.")
        return value
    if isinstance(value, dict):
        return {key: _rewrite_refs(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_refs(item, mapping) for item in value]
    return value


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


def expand_subgraphs(graph: WorkflowGraph, *, _depth: int = 0) -> WorkflowGraph:
    """Replace every ``subgraph`` node with the contents of the graph it names.

    **Idempotent**, and that matters: the referencing node is kept after
    expansion (it is the entry adapter), so without the marker a second call would
    inline the same sub-flow again and produce duplicate node ids. Callers that
    expand and then compile — or compile an already-expanded graph — would fail
    with a validator error that points at the symptom rather than the cause.
    """
    if _depth > 16:
        raise SubgraphError(
            "Sub-flow nesting is too deep (more than 16 levels). "
            "This usually means a reference loop."
        )

    # Checked explicitly, and only at the top of the walk.
    #
    # Left to the recursion, a reference loop fails as `RecursionError` — a stack
    # trace that says nothing about which two workflows point at each other. The
    # check costs one walk and turns that into a message naming the chain.
    if _depth == 0:
        cycle = chain_has_cycle(graph)
        if cycle:
            raise SubgraphError(
                "Sub-flow reference loop: " + " -> ".join(cycle) + ". "
                "A workflow cannot contain itself, directly or through others."
            )

    if not any(
        n.type == SUBGRAPH_TYPE and not (n.config or {}).get(EXPANDED_MARKER)
        for n in graph.nodes
    ):
        return graph

    nodes: list[NodeSpec] = []
    edges: list[EdgeSpec] = []
    #: referencing node id -> ids inside the sub-flow that produce its result.
    #: The parent's outgoing edges are re-sourced from these.
    exits: dict[str, list[str]] = {}

    for node in graph.nodes:
        if node.type != SUBGRAPH_TYPE or (node.config or {}).get(EXPANDED_MARKER):
            nodes.append(node)
            continue
        child = _expanded_child(node)
        exits[node.id] = _inline(node, child, nodes, edges)

    for edge in graph.edges:
        if edge.source in exits:
            # `h -> next` becomes `sub-flow's last node(s) -> next`, so the next
            # node reads `vars[<last node>]` and gets the sub-flow's result.
            #
            # When a sub-flow has several nodes feeding its End they all become
            # exits; every one of them is wired to the same target rather than
            # picking one, so no branch is silently dropped.
            for index, source in enumerate(exits[edge.source] or [edge.source]):
                edges.append(
                    edge.model_copy(
                        update={
                            "id": f"{edge.id}~out{index}" if index else edge.id,
                            "source": source,
                        }
                    )
                )
            continue
        edges.append(edge)

    return graph.model_copy(update={"nodes": nodes, "edges": edges})


def _expanded_child(node: NodeSpec) -> WorkflowGraph:
    """The referenced graph, expanded itself, so nesting is handled bottom-up."""
    return expand_subgraphs(resolve(node), _depth=1)


def _inline(
    node: NodeSpec,
    child: WorkflowGraph,
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
) -> list[str]:
    """Append ``child``'s contents to ``nodes``/``edges``, namespaced under ``node``.

    Returns the ids inside the sub-flow that produce its result (its End's
    feeders, namespaced).

    The referencing node is **kept** and acts as the entry adapter: its incoming
    edges stay as they are, and the sub-flow's first nodes read their input from
    ``vars[node.id]`` — which is exactly what they read from ``vars[start]`` inside
    their own graph, because that is how this compiler resolves inputs. That is
    what makes inlining work without inventing a node type.
    """
    prefix = f"{node.id}{NAMESPACE_SEPARATOR}"
    structural = [n for n in child.executable_nodes() if n.type in STRUCTURAL_TYPES]
    body = [n for n in child.executable_nodes() if n.type not in STRUCTURAL_TYPES]
    start_ids = {n.id for n in structural if n.type == "start"}
    end_ids = {n.id for n in structural if n.type == "end"}

    # Which of the sub-flow's nodes produce its result, worked out **before**
    # anything is appended: an exit node needs a config flag, and the config has
    # to be written when the node is created rather than patched afterwards.
    exit_ids = {
        edge.source
        for edge in child.edges
        if edge.target in end_ids and edge.source not in start_ids
    }

    # Every node id inside the sub-flow, mapped to its namespaced form. The
    # sub-flow's own Start is dropped, so references to it resolve to the
    # referencing node — which holds exactly the input Start used to.
    mapping: dict[str, str] = {n.id: prefix + n.id for n in body}
    for start_id in start_ids:
        mapping[start_id] = node.id

    nodes.append(
        node.model_copy(update={"config": {**(node.config or {}), EXPANDED_MARKER: True}})
    )
    for child_node in body:
        config = _rewrite_refs(child_node.config or {}, mapping)
        if child_node.id in exit_ids:
            config = {**config, RESULT_FOR_KEY: node.id}
        nodes.append(
            child_node.model_copy(update={"id": prefix + child_node.id, "config": config})
        )

    exits: list[str] = []

    for index, edge in enumerate(child.edges):
        source_is_start = edge.source in start_ids
        target_is_end = edge.target in end_ids

        if source_is_start and target_is_end:
            # A sub-flow that is only Start -> End contributes nothing to run.
            continue
        if source_is_start:
            # Parent wiring feeds this edge instead of the sub-flow's Start.
            # The **target** still needs the prefix: it names a node from inside
            # the sub-flow, and without it the edge points at a node that does not
            # exist in the expanded graph.
            edges.append(
                edge.model_copy(
                    update={
                        "id": f"{node.id}~in~{index}",
                        "source": node.id,
                        "target": prefix + edge.target,
                    }
                )
            )
            continue
        if target_is_end:
            # The sub-flow's End is dropped; whatever fed it *is* the result.
            exits.append(prefix + edge.source)
            continue
        if edge.source in end_ids:
            # Nothing leaves an End node. A sub-flow with such an edge is already
            # invalid on its own, and its own validation says so.
            continue
        edges.append(
            edge.model_copy(
                update={
                    "id": f"{node.id}~{index}",
                    "source": prefix + edge.source,
                    "target": prefix + edge.target,
                }
            )
        )

    return exits


def pin_references(graph: WorkflowGraph) -> WorkflowGraph:
    """Record the current digest on every reference that has none.

    Called on the *save* path. Pinning at save time is what makes the guarantee
    real without any extra step for the user: from the moment a graph is saved,
    editing the sub-workflow it uses is reported instead of silently changing what
    this graph does.
    """
    if not any(n.type == SUBGRAPH_TYPE for n in graph.nodes):
        return graph

    changed = False
    nodes: list[NodeSpec] = []
    for node in graph.nodes:
        if node.type != SUBGRAPH_TYPE:
            nodes.append(node)
            continue
        ref = reference_of(node)
        child = load_subgraph(ref) if ref else None
        if child is None:
            # Leave it alone: validation reports the missing reference, and
            # pinning a hash for a definition that does not exist would be a lie.
            nodes.append(node)
            continue
        digest = definition_digest(child)
        if pinned_digest(node) == digest:
            nodes.append(node)
            continue
        changed = True
        nodes.append(
            node.model_copy(update={"config": {**(node.config or {}), REF_HASH_KEY: digest}})
        )

    return graph.model_copy(update={"nodes": nodes}) if changed else graph


__all__ = [
    "EXPANDED_MARKER",
    "NAMESPACE_SEPARATOR",
    "REF_HASH_KEY",
    "REF_KEY",
    "RESULT_FOR_KEY",
    "SUBGRAPH_TYPE",
    "STRUCTURAL_TYPES",
    "SubgraphError",
    "chain_has_cycle",
    "clear_subgraph_provider",
    "definition_digest",
    "digest_matches",
    "expand_subgraphs",
    "has_subgraph_provider",
    "list_subgraph_options",
    "load_subgraph",
    "pin_references",
    "pinned_digest",
    "reference_of",
    "resolve",
    "set_subgraph_provider",
]
