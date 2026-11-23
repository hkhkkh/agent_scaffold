"""Compiler: ``WorkflowGraph`` -> LangGraph ``StateGraph``.

This is where the editor's intent becomes an executable program. Three decisions
shape the whole module:

**1. Nodes write to shared state, edges carry dependencies.**
Each node's return value is stored as ``vars[node_id]``. A node's inputs are
resolved by reading its predecessors' entries, so a node can read any ancestor,
not just its immediate parent — which is what makes branch-and-merge and loops
work at all.

**2. Branching nodes become conditional edges.**
Condition and Loop nodes cannot use ordinary edges: which successor runs depends
on a runtime decision. They are compiled to ``add_conditional_edges`` keyed on the
branch the node recorded in state. Notably the node *still executes*, so its
decision shows up in the timeline — that observability is worth one super-step.

**3. Multiple edges into one input handle arrive as a list.**
This is what makes a Variable Aggregator work: it merges the outputs of mutually
exclusive branches without needing a special "collect" input type.

Note: this module deliberately avoids ``from __future__ import annotations``.
LangGraph inspects node signatures at runtime to validate the ``config``
parameter, and stringised annotations defeat that check.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from app.graph.schema import NodeSpec, WorkflowGraph
from app.graph.state import build_state_schema
from app.graph.subgraphs import RESULT_FOR_KEY, SubgraphError, expand_subgraphs
from app.graph.validate import validate_graph
from app.nodes import create_node_instance, effective_inputs
from app.nodes.base import NodeContext
from app.nodes.condition import BRANCH_KEY
from app.nodes.control import LOOP_BRANCH_KEY
from app.nodes.supervisor import DECISION_KEY
from app.runtime.attempts import ERROR_KEY, AttemptPolicy, run_with_policy
from app.runtime.budget import get_budget
from app.runtime.projection import WIND_DOWN_STOP, WindDown, project, wind_down_mode
from app.runtime.serialize import redact
from app.settings import get_settings

#: Node types whose successors are chosen at runtime rather than fixed by edges.
ROUTING_TYPES = frozenset({"condition", "loop"})

#: Node types that decide their successor by returning `Command(goto=...)`.
#:
#: Separate from `ROUTING_TYPES` because the mechanism differs and so does the
#: compiler's job: a routing node records a branch flag that a conditional edge
#: reads, while these *return the destination themselves*. The distinction that
#: costs real bugs is that a `Command` **adds** an edge instead of replacing one,
#: so their static edges must not be drawn at all — see the edge loop below.
DYNAMIC_ROUTING_TYPES = frozenset({"supervisor"})

#: Key a node uses to mark a value as the workflow's result rather than a
#: regular output. Consumed by the End node.
OUTPUT_KEY = "__output__"

#: ``await emit(kind, data, node_id, node_label)`` — how a node reports progress.
#: ``kind`` is one of ``node_started`` / ``node_finished`` / ``node_failed`` /
#: ``chunk`` / ``log``.
EmitterFn = Callable[[str, dict[str, Any], str, str], Awaitable[None]]


async def _null_emitter(
    _kind: str, _data: dict[str, Any], _node_id: str, _node_label: str
) -> None:
    """Default emitter for compilation outside a run (tests, validation)."""
    return None


class CompileError(RuntimeError):
    """Raised when a graph cannot be turned into an executable program."""


CompiledGraph = Any  # langgraph's CompiledStateGraph, kept loose on purpose


def compile_graph(
    graph: WorkflowGraph,
    *,
    checkpointer: Any | None = None,
    emitter: EmitterFn | None = None,
    strict: bool = True,
) -> CompiledGraph:
    """Compile a workflow into a runnable LangGraph graph.

    ``checkpointer`` is what enables human-in-the-loop: without it an
    ``interrupt()`` cannot be resumed. The runtime supplies one by default.

    ``emitter`` receives node lifecycle events. Omit it for tests and
    validation, where there is nothing to report to.

    The emitter is called as ``emitter(kind, data, node_id, node_label)``.

    Sub-flow nodes are expanded into this graph first, so everything below sees
    one flat graph and none of it has to know sub-flows exist.
    """
    emit_raw: EmitterFn = emitter or _null_emitter

    try:
        graph = expand_subgraphs(graph)
    except SubgraphError as exc:
        raise CompileError(str(exc)) from exc

    report = validate_graph(graph)
    if strict and not report.ok:
        messages = "; ".join(i.message for i in report.errors)
        raise CompileError(f"Workflow is not valid: {messages}")

    executable = graph.executable_nodes()
    if not executable:
        raise CompileError("Workflow has no nodes to run.")

    node_index = {node.id: node for node in graph.nodes}
    state_schema = build_state_schema(graph.state_schema)

    builder: StateGraph = StateGraph(state_schema)

    # ---------------------------------------------------------------- nodes
    for node in executable:
        builder.add_node(node.id, _make_node_fn(graph, node, node_index, emit_raw))

    # ---------------------------------------------------------------- edges
    entry = next((n for n in executable if n.type == "start"), None)
    if entry is None:
        if strict:
            raise CompileError("Workflow has no Start node.")
        # Non-strict mode exists so the UI can preview an unfinished graph.
        # Falling back to a node with nothing feeding it keeps the preview
        # meaningful instead of refusing to build anything at all.
        entry = _infer_entry(executable, graph)
        if entry is None:
            raise CompileError("Workflow has no node that can act as an entry point.")

    # Fan-in is usually a join, but mutually exclusive branches are different.
    #
    # `builder.add_edge(source, target)` called once per source makes LangGraph
    # treat each edge as a separate trigger: the target runs again for every
    # upstream branch that finishes. Correct for a map step, wrong for a
    # converging workflow — measured on a 19-node hospital graph, a `prompt` with
    # four upstream branches ran three times, so the downstream email was sent
    # three times. Passing the source list in one call gives the intended "run
    # after all of these are done" semantics.
    #
    # The exception is a set of targets produced by the same routing node (the
    # two handles of one Condition). Only one of them can run, so a join would
    # wait forever; those keep one edge per source.
    joins: dict[str, list[str]] = {}
    exclusive_groups: list[set[str]] = []
    for node in executable:
        if node.type in ROUTING_TYPES:
            targets = {edge.target for edge in graph.outgoing(node.id)}
            if len(targets) > 1:
                exclusive_groups.append(targets)

    executable_ids = {node.id for node in executable}
    by_id = {node.id: node for node in executable}

    for node in executable:
        outgoing = graph.outgoing(node.id)

        if node.type in ROUTING_TYPES:
            _add_routing_edges(builder, graph, node, outgoing, node_index)
            continue

        if node.type in DYNAMIC_ROUTING_TYPES:
            # ★ No static edges out of a dynamic router, and this is not tidiness.
            #
            # From the LangGraph docs: "`Command` only adds dynamic edges — static
            # edges defined with `add_edge` still execute". A supervisor that
            # returns `Command(goto=X)` while also wired to Y would run **both**,
            # which looks like parallelism and is a bug. So the author's outgoing
            # edges are consumed as the *declaration* of the worker set (read
            # below) and then deliberately not drawn.
            _add_supervisor_edges(builder, graph, node, outgoing, node_index)
            continue

        if not outgoing:
            # Terminal by construction. `end` nodes close the run; anything else
            # simply stops, which the validator has already warned about.
            if node.type != "end":
                builder.add_edge(node.id, END)
            continue

        for edge in outgoing:
            if edge.target not in executable_ids:
                continue
            sources = joins.setdefault(edge.target, [])
            if node.id not in sources:
                sources.append(node.id)

    for target, sources in joins.items():
        if len(sources) == 1:
            builder.add_edge(sources[0], target)
            continue
        # Loop graphs keep the old one-edge-per-source wiring. The back edge into
        # the loop head is a trigger, not a join: waiting for it would deadlock
        # on the first iteration. The join rule is for acyclic parallel branches.
        if any(node.type == "loop" for node in executable):
            for source in sources:
                builder.add_edge(source, target)
            continue
        source_set = set(sources)
        mutually_exclusive = any(source_set <= group for group in exclusive_groups)
        target_node = by_id.get(target)
        aggregator_or = bool(
            target_node is not None
            and target_node.type == "aggregator"
            and (target_node.config or {}).get("mode", "first_non_null")
            in {"first_non_null", "strict_single"}
        )
        if mutually_exclusive or aggregator_or:
            for source in sources:
                builder.add_edge(source, target)
        else:
            builder.add_edge(sources, target)

    builder.add_edge(START, entry.id)

    return builder.compile(checkpointer=checkpointer) if checkpointer else builder.compile()


def _infer_entry(nodes: list[NodeSpec], graph: WorkflowGraph) -> NodeSpec | None:
    """Pick a plausible entry point for a graph that has no Start node.

    Used only in non-strict (preview) compilation. Prefers a node with no
    incoming connections, which is what a user drawing a flow usually means.
    """
    targeted = {e.target for e in graph.edges}
    roots = [n for n in nodes if n.id not in targeted]
    if roots:
        return roots[0]
    return nodes[0] if nodes else None


# ---------------------------------------------------------------------------
# Node wrapper
# ---------------------------------------------------------------------------


def _make_node_fn(
    graph: WorkflowGraph,
    node: NodeSpec,
    node_index: dict[str, NodeSpec],
    emit_raw: EmitterFn,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Wrap a :class:`BaseNode` into a LangGraph node function.

    The emitter is captured in this closure rather than looked up from a context
    variable at execution time. LangGraph may run nodes in tasks of its own
    making, and a context variable that silently fails to propagate would turn
    every event into a no-op — a bug that only shows up as a dead-looking UI.
    Binding it here makes delivery unconditional.

    The node signature takes ``state`` only; run-scoped infrastructure is
    retrieved with :func:`get_config`. That keeps the contract about *data* and
    avoids LangGraph's signature-validation warning.
    """
    instance = create_node_instance(node)
    incoming = graph.incoming(node.id)
    node_label = node.label or node.id

    # Bind this node's identity once, so every event it produces is attributed
    # without the node code having to know its own id.
    async def emit(kind: str, data: dict[str, Any]) -> None:
        await emit_raw(kind, data, node.id, node_label)

    async def run_node(state: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()

        # The run's budget, or `None` when this graph is compiled outside a run
        # (tests, the validator, single-node debugging).
        budget = get_budget(_current_run_id())
        if budget is not None:
            # Before the node: this is the check that actually stops the spend.
            # Without it the last node is free to spend everything.
            budget.check()
            # The *forecast*, as opposed to the ceiling above. The ceiling stops a
            # run that has already overrun; this notices that it is about to, while
            # there is still allowance to finish with. It never raises — the
            # policy in `_wind_down` decides what to do about it.
            await _report_projection(budget, settings, emit)

        inputs = _collect_inputs(state, incoming, node_index)
        merged_config = _merge_tweaks(node, _current_config())

        # Time budget and retry count. Resolved per call rather than bound at
        # compile time because a *tweak* can only arrive with the run config,
        # which does not exist until LangGraph invokes this function.
        policy = AttemptPolicy.resolve(merged_config, settings)

        await emit("node_started", {"inputs": inputs, "config": redact(merged_config)})

        ctx = NodeContext(
            node=node,
            config=merged_config,
            inputs=inputs,
            state=state,
            settings=settings,
            emit=lambda payload: emit("chunk", payload),
            log=lambda message, level="info": emit(
                "log", {"message": message, "level": level}
            ),
        )

        async def on_retry(attempt: int, exc: BaseException) -> None:
            # Only the *final* failure produces a `node_failed` event; a retry is
            # a note on the timeline, not an outcome. Without this line a
            # retried node looks like one slow node.
            await emit(
                "log",
                {
                    "message": (
                        f"Attempt {attempt} of {policy.attempts_allowed} failed "
                        f"({type(exc).__name__}: {exc}). Retrying."
                    ),
                    "level": "warning",
                },
            )

        started = time.perf_counter()
        try:
            result = await run_with_policy(
                lambda: instance.run(ctx),
                policy=policy,
                label=node_label,
                # A pause is not a failure. `GraphBubbleUp` (and its
                # `GraphInterrupt`) has to reach the engine on the first raise,
                # or human-in-the-loop would ask the same person repeatedly.
                never_retry=(GraphBubbleUp,),
                on_retry=on_retry,
            )
        except GraphBubbleUp:
            # Control-flow exceptions (notably `GraphInterrupt`, raised by
            # `interrupt()` in the Human Input node) must reach the engine
            # untouched. Catching one here would convert a pause into a failure
            # and silently break human-in-the-loop.
            raise
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            detail = f"{type(exc).__name__}: {exc}"

            # `on_error` decides whether this node's failure ends the run. It is
            # per-node rather than global because only the author knows which
            # steps are allowed to fail — a summariser that finds nothing is not
            # a scheduling problem, while a payment step failing is.
            if not policy.continues_on_error:
                await emit("node_failed", {"error": detail, "duration_ms": elapsed})
                # Re-raise with node context so the run timeline names the culprit.
                raise RuntimeError(
                    f"[{node.label or node.id}] {detail}"
                ) from exc

            # The run continues. Record the failure where a downstream node and
            # the timeline can both see it, and write whatever value the author
            # nominated so the next node reads something usable rather than a
            # marker it would have to know how to interpret.
            failure = {
                "out": policy.failure_value,
                ERROR_KEY: {
                    "error": detail,
                    "node_id": node.id,
                    "node_label": node.label or node.id,
                },
            }
            # The record goes out on the *failed* event, not a later `finished`
            # one. Measured: without this the step row showed the failure but its
            # `outputs` were null, so the run timeline could say a node failed and
            # not what it produced instead.
            await emit(
                "node_failed",
                {"error": detail, "outputs": failure, "duration_ms": elapsed},
            )
            await emit(
                "log",
                {
                    "message": (
                        f"Node failed but on_error={policy.on_error}, so the run "
                        f"continues without it ({detail})."
                    ),
                    "level": "warning",
                },
            )
            result = failure

        if not isinstance(result, dict):
            result = {"out": result}

        if budget is not None:
            # After the node, and this is not a duplicate of the check above.
            # One node can be expensive on its own — an agent loop, a long model
            # call — so the check that runs *next* would already be too late by
            # that node's cost. Reporting here bounds the overshoot to one node.
            budget.check()

        elapsed = (time.perf_counter() - started) * 1000

        # The End node marks the workflow result explicitly.
        output = result.pop(OUTPUT_KEY, None)
        update: dict[str, Any] = {"vars": {node.id: result}}
        if output is not None:
            update["output"] = output

        # A sub-flow's exit node also records its output under the id of the node
        # that referenced the sub-flow, so `{{ vars.<sub-flow node>.out }}` and the
        # re-sourced edge agree on what the sub-flow's result is. See
        # `graph/subgraphs.py` for why both have to exist.
        result_for = (node.config or {}).get(RESULT_FOR_KEY)
        if isinstance(result_for, str) and result_for:
            update["vars"][result_for] = result

        # A node that failed but was allowed to continue must not also announce
        # that it finished. Measured: the `node_failed` event above was emitted,
        # then overwritten by this one, so the timeline showed a failed step as
        # `succeeded` and the only trace of the failure was a warning line. That
        # is the exact "looks like it worked" failure the `stop` default exists to
        # prevent, reintroduced one level down.
        if isinstance(result.get(ERROR_KEY), dict):
            return update

        await emit("node_finished", {"outputs": result, "duration_ms": elapsed})

        # ★ After a node succeeded, the run may be asked to stop while it still
        # can. This is the `stop` wind-down: the node's work is kept, and the run
        # ends here instead of spending the rest of its allowance on the remaining
        # steps. Raising is how a LangGraph node ends a run, and it is why the
        # forecast has to be computed *before* the limit rather than at it.
        if budget is not None and wind_down_stops(budget, settings):
            await emit(
                "log",
                {
                    "message": (
                        "Stopping before the budget runs out, keeping what this "
                        "run produced (BUDGET_WIND_DOWN=stop)."
                    ),
                    "level": "warning",
                },
            )
            raise WindDown(project(budget).reasons)

        return update

    return run_node


def _current_config() -> RunnableConfig:
    """Read the active run configuration from LangGraph's context.

    Returns an empty dict when called outside a graph run (e.g. from a unit test
    that invokes a node directly), so nodes stay testable in isolation.
    """
    try:
        from langgraph.config import get_config

        return get_config() or {}
    except Exception:  # noqa: BLE001 - absence of a run context is not an error
        return {}


def _current_run_id() -> str | None:
    """The id of the run this node belongs to, or ``None`` outside a run.

    The manager puts it in `configurable`; see `runtime/budget.py` for why the
    budget itself is looked up from a registry rather than passed through here.
    """
    configurable = _current_config().get("configurable") or {}
    value = configurable.get("run_id")
    return value if isinstance(value, str) else None


async def _report_projection(budget: Any, settings: Any, emit: EmitterFn) -> None:
    """Put the forecast on the timeline when a limit is getting close.

    An event rather than a log line, so a client can render it as a warning
    without parsing prose. Emitted once per node that is near a limit, which is
    the granularity at which the reader can act.
    """
    projection = project(budget)
    if not projection.should_wind_down:
        return
    await emit(
        "log",
        {
            "message": "Budget nearly spent: " + "; ".join(projection.reasons),
            "level": "warning",
            "projection": projection.as_dict(),
        },
    )


def wind_down_stops(budget: Any, settings: Any) -> bool:
    """Whether this run should end now rather than spend its last allowance.

    Split from the raise site so a test can assert the decision without building
    a graph, and so the two conditions — mode is `stop`, and the forecast says so
    — are visible together rather than buried in an `if`.
    """
    if wind_down_mode(settings) != WIND_DOWN_STOP:
        return False
    return project(budget).should_wind_down


def _collect_inputs(
    state: dict[str, Any],
    incoming: list[Any],
    node_index: dict[str, NodeSpec],
) -> dict[str, Any]:
    """Resolve this node's input handles from upstream outputs.

    When several edges land on one handle the values are collected into a list,
    which is what lets an aggregator merge branches.
    """
    variables = state.get("vars") or {}
    collected: dict[str, list[Any]] = {}

    for edge in incoming:
        source = node_index.get(edge.source)
        if source is None:
            continue
        entry = variables.get(edge.source)
        value = _value_from(entry, source, edge.source_handle)
        collected.setdefault(edge.target_handle or "in", []).append(value)

    resolved: dict[str, Any] = {}
    for handle, values in collected.items():
        present = [v for v in values if v is not None]
        if len(values) == 1:
            resolved[handle] = values[0]
        else:
            resolved[handle] = present
    return resolved


def _value_from(entry: Any, source: NodeSpec, handle: str | None) -> Any:
    """Read one output value out of a node's stored result.

    Routing nodes return branch flags (``{"yes": True, ...}``); what a downstream
    node actually wants is the value that flowed *through* them, so they are read
    from their forwarded ``out`` instead.
    """
    if not isinstance(entry, dict):
        return entry if handle in (None, "out") else None
    if source.type in ROUTING_TYPES:
        return entry.get("out")
    return entry.get(handle or "out")


def _merge_tweaks(node: NodeSpec, config: RunnableConfig | None) -> dict[str, Any]:
    """Apply per-run config overrides (Langflow calls these "tweaks")."""
    merged = dict(node.config or {})
    if not isinstance(config, dict):
        return merged
    tweaks = (config.get("configurable") or {}).get("tweaks") or {}
    overrides = tweaks.get(node.id)
    if isinstance(overrides, dict):
        merged.update(overrides)
    return merged


# ---------------------------------------------------------------------------
# Routing edges
# ---------------------------------------------------------------------------


def _add_routing_edges(
    builder: StateGraph,
    graph: WorkflowGraph,
    node: NodeSpec,
    outgoing: list[Any],
    node_index: dict[str, NodeSpec],
) -> None:
    """Compile a Condition or Loop node into conditional edges.

    The mapping keys are the node's *output handle ids*, which is why the handle
    ids on those node types are load-bearing: a branch id must match a handle id
    for the wiring to line up.
    """
    if not outgoing:
        builder.add_edge(node.id, END)
        return

    targets: dict[str, str] = {}
    for edge in outgoing:
        handle = edge.source_handle
        if handle is None:
            # An unlabelled edge from a routing node is ambiguous; treat it as
            # the fallback path rather than silently dropping it.
            handle = "done" if node.type == "loop" else "no"
        targets[handle] = edge.target

    key = BRANCH_KEY if node.type == "condition" else LOOP_BRANCH_KEY

    def router(state: dict[str, Any]) -> str:
        entry = (state.get("vars") or {}).get(node.id) or {}
        chosen = entry.get(key)
        if chosen in targets:
            return chosen
        # Defensive: a missing decision should still go somewhere sensible
        # instead of raising deep inside the engine.
        for fallback in ("no", "done", "out", "else"):
            if fallback in targets:
                return fallback
        return next(iter(targets.values()))

    builder.add_conditional_edges(node.id, router, targets)


def _add_supervisor_edges(
    builder: StateGraph,
    graph: WorkflowGraph,
    node: NodeSpec,
    outgoing: list[Any],
    node_index: dict[str, NodeSpec],
) -> None:
    """Compile a Supervisor node into conditional edges keyed on its choice.

    Two things are load-bearing here.

    **1. The static edges are consumed as a declaration and not drawn.** The
    author's outgoing edges say "this supervisor may pick among these". Drawing
    them as real edges would be actively wrong — a `Command` adds an edge rather
    than replacing one, so a handoff to X would also run every wired target. The
    doc is explicit: use either `Command` or static edges, not both.

    **2. The declared set is the union of the author's edges and the node's own
    `workers` parameter.** Both are legitimate ways to say it, and a graph that
    lists workers only in the config is easier to build than one that also needs
    wires drawn. Taking the union means neither spelling is a mistake the user
    has to discover.

    "done" always gets a target so the supervisor can stop; without it a
    supervisor that decides the work is finished would have nowhere to go.
    """
    config = node.config or {}
    declared: list[str] = []

    raw_workers = config.get("workers")
    if isinstance(raw_workers, str):
        raw_workers = [part.strip() for part in raw_workers.replace(",", "\n").splitlines()]
    if isinstance(raw_workers, (list, tuple)):
        declared.extend(str(item).strip() for item in raw_workers if str(item).strip())

    for edge in outgoing:
        if edge.target in node_index and edge.target not in declared:
            declared.append(edge.target)

    targets: dict[str, str] = {
        name: name for name in declared if name in node_index
    }
    # The stop destination. `END` is the string LangGraph uses for it.
    targets["done"] = END

    key = DECISION_KEY

    def router(state: dict[str, Any]) -> str:
        entry = (state.get("vars") or {}).get(node.id) or {}
        chosen = entry.get(key)
        if chosen in targets:
            return chosen
        # A supervisor that chose something unroutable has already said so in its
        # log; going to `done` finishes the run with what it has rather than
        # raising from inside the engine.
        return "done"

    builder.add_conditional_edges(node.id, router, targets)


__all__ = [
    "CompileError",
    "CompiledGraph",
    "DYNAMIC_ROUTING_TYPES",
    "OUTPUT_KEY",
    "ROUTING_TYPES",
    "compile_graph",
]
