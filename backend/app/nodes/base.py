"""Node execution primitives.

A node type is:
  * a :class:`NodeTypeSpec` (declarative: ports + params + metadata), and
  * a :class:`BaseNode` subclass implementing :meth:`BaseNode.run`.

The declaration drives the canvas, the Inspector form, and the AI prompts. The
implementation is a plain async function over :class:`NodeContext`.

Contract every runner must honour
---------------------------------
1. Return a ``dict`` keyed by **output handle id**: ``{"out": value}``.
   The runtime stores it as ``state["vars"][node_id]`` so downstream nodes read
   it via ``ctx.var("<node_id>", "out")``.
2. Be **idempotent**. With a checkpointer configured, resuming after an
   ``interrupt()`` re-runs the node *from the top of the function*; any side
   effect before the pause happens again.
3. Never mutate ``ctx.state`` in place — return an update instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, ClassVar

from app.graph.schema import NodeSpec, NodeTypeSpec, StateField, StringParam
from app.settings import Settings

#: ``await ctx.emit({"progress": 50})`` — pushes arbitrary JSON to the client.
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]

#: ``await ctx.log("message", "info")`` — first arg is the text, second the level
#: (``"info" | "warning" | "error"``). Levels colour the run timeline.
LogFn = Callable[[str, str], Awaitable[None]]


@dataclass
class NodeContext:
    """Everything a node implementation may touch during one execution."""

    node: NodeSpec
    config: dict[str, Any]
    """Merged config: param defaults <- node config <- per-run tweaks."""

    inputs: dict[str, Any]
    """Upstream values keyed by *this* node's input handle id."""

    state: dict[str, Any]
    """Full graph state. Treat as read-only."""

    settings: Settings
    emit: EmitFn
    """Push a custom event to the client (progress, partial results, ...)."""

    log: LogFn
    """``await ctx.log(text, level)`` — append a line to the run timeline."""

    node_outputs: dict[str, dict[str, Any]] | None = None
    """Per-node output overrides supplied by single-node debugging."""

    # ------------------------------------------------------------- accessors
    @property
    def vars(self) -> dict[str, Any]:
        value = self.state.get("vars")
        return value if isinstance(value, dict) else {}

    def input(self, handle: str, default: Any = None) -> Any:
        """Value arriving on an input handle (``None`` when unconnected)."""
        value = self.inputs.get(handle, default)
        return default if value is None else value

    def var(self, node_id: str, handle: str = "out", default: Any = None) -> Any:
        """Read another node's output directly from shared state."""
        if self.node_outputs and node_id in self.node_outputs:
            entry = self.node_outputs[node_id]
        else:
            entry = self.vars.get(node_id)
        if isinstance(entry, dict):
            value = entry.get(handle, default)
            return default if value is None else value
        return entry if handle == "out" and entry is not None else default

    @property
    def messages(self) -> list[Any]:
        value = self.state.get("messages")
        return value if isinstance(value, list) else []

    def require(self, key: str) -> Any:
        """Fetch a required config value, failing loudly with a useful message."""
        value = self.config.get(key)
        if value in (None, "", [], {}):
            raise ValueError(
                f"Node '{self.node.label or self.node.id}' ({self.node.type}) "
                f"is missing required setting '{key}'."
            )
        return value


class BaseNode:
    """Base class for all node implementations."""

    spec: ClassVar[NodeTypeSpec]

    def __init__(self, node: NodeSpec) -> None:
        self.node = node

    # ---------------------------------------------------------------- config
    @staticmethod
    def defaults_for(spec: NodeTypeSpec) -> dict[str, Any]:
        """Collect declared parameter defaults for a node type."""
        out: dict[str, Any] = {}
        for param in spec.params:
            default = getattr(param, "default", None)
            if default is not None:
                out[param.name] = default
        return out

    def resolve_config(self, ctx: NodeContext) -> dict[str, Any]:
        """Param defaults, then saved node config, then per-run tweaks."""
        merged = self.defaults_for(self.spec)
        merged.update(ctx.config or {})
        return merged

    # ------------------------------------------------------------------ state
    def state_fields(self) -> list[StateField]:
        """Optional extra shared-state channels this node needs.

        Most nodes write into ``vars``; this hook exists for nodes that need a
        typed channel with its own reducer.
        """
        return []

    # ------------------------------------------------------------------- run
    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        """Execute and return ``{output_handle_id: value}``."""
        raise NotImplementedError(f"{type(self).__name__}.run() is not implemented")


def make_string_param(
    name: str,
    label: str,
    *,
    description: str = "",
    default: str = "",
    required: bool = False,
    multiline: bool = False,
    **kwargs: Any,
) -> StringParam:
    """Small helper that keeps node declarations readable."""
    return StringParam(
        name=name,
        label=label,
        description=description,
        default=default,
        required=required,
        multiline=multiline,
        **kwargs,
    )


#: Keys :func:`template_scope` owns. State channels with these names do not get
#: merged over the top, because the node's own values are what the documented
#: placeholders mean.
_SCOPE_OWNED = ("vars", "inputs", "params")


def context_text(ctx: NodeContext) -> str:
    """What arrived on the node's context port, as text a model can read.

    A string passes through untouched — a prompt-shaped value should not acquire
    JSON quoting on the way. Anything else is serialised, because a model reads
    ``{"question": "..."}`` far more faithfully than a Python repr.

    Lives here rather than inside one node because every model-facing node needs
    the same fallback: a connected node must always have something to work with,
    whether or not the author wrote a template that happens to reference it.
    """
    import json

    value = ctx.inputs.get("in")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str, indent=2)
    except (TypeError, ValueError):
        return str(value)


def template_scope(ctx: NodeContext, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The namespace a node's ``{{ }}`` placeholders resolve against.

    One function rather than a copy in every node, because the interesting part is
    a precedence rule that eight copies had already got wrong the same way:

        scope = {"inputs": ctx.inputs, ...}
        scope.update({k: v for k, v in ctx.state.items() if k != "vars"})   # wrong

    ``state`` carries a channel named ``inputs`` — the run's declared inputs from
    the Start node — so that ``update`` **overwrote the node's own input handles**
    with the whole run's inputs. ``{{ inputs.in }}``, which every input port's
    description advertises, therefore resolved to the run inputs and came out
    empty, while ``ctx.input("in")`` returned the real value. Two ways to read one
    thing, disagreeing.

    ``inputs`` / ``vars`` / ``params`` are the names the node owns; every other
    state channel is merged in, so ``{{ some_shared_field }}`` keeps working.

    The run's own inputs are merged in last, **by name and only into unclaimed
    names** — see the loop below for why that has to exist.
    """
    scope: dict[str, Any] = {
        "inputs": ctx.inputs,
        "vars": ctx.vars,
        "params": config if config is not None else ctx.config,
        "state": ctx.state,
    }
    for key, value in ctx.state.items():
        if key not in _SCOPE_OWNED:
            scope[key] = value

    # `{{ query }}` resolves to the `query` this run was started with.
    #
    # `setdefault` rather than an assignment, and that is the whole point: the
    # bug described above was `scope["inputs"] = run_inputs`, which *replaced* the
    # node's handles. Filling only unclaimed names leaves `{{ inputs.in }}`,
    # `{{ vars.* }}` and declared state channels winning.
    #
    # It has to exist because the prompt-bearing node types default to
    # `{{ query }}` while the Start node defaults to declaring `query`, and the
    # run's inputs live in the `inputs` *channel*. Measured (2026-10-09): without
    # this the default pairing rendered `{{ query }}` as empty, so the model was
    # sent an empty user message and replied "I'm ready to help!" — the defect the
    # canvas browser test recorded. It also explains why `{{ inputs.query }}`
    # worked in single-node debugging (which merges hand-supplied inputs into the
    # handles) and not in a real run: two ways to read one value that disagreed.
    run_inputs = ctx.state.get("inputs")
    if isinstance(run_inputs, dict):
        for key, value in run_inputs.items():
            scope.setdefault(key, value)
    return scope


def render_template(template: str, ctx: NodeContext, config: dict[str, Any]) -> str:
    """Resolve a node's ``{{ }}`` placeholders through the shared scope.

    One function rather than a copy per node. It was three copies — the supervisor
    and both memory nodes — and they were **identical**, which is the same shape of
    mistake the docstring above records: bodies that look like they want to be
    separate, so the next change lands in one of them and the other two silently
    keep the old behaviour.

    Routed through :func:`template_scope` rather than reading ``ctx.state`` here,
    because that helper is where the precedence rule lives.

    The ``"{{" not in template`` shortcut is load-bearing: it keeps a literal
    prompt — the common case — from paying to build a scope, and it is why a
    template with no placeholders comes back byte-identical.
    """
    from app.runtime.template import render

    if "{{" not in template:
        return template
    return render(template, template_scope(ctx, config))


def current_run_id() -> str:
    """The run this node is executing in, or ``""``.

    Read from LangGraph's ``configurable``, the way ``app/graph/compiler.py`` reads
    it for the budget. A node has no other channel to the run id, and the
    alternative — carrying it through state — would put a field on every graph
    that only the memory nodes want.

    Shared for the same reason as :func:`render_template`: it was two identical
    copies. ``""`` rather than raising when called outside a graph, so a unit test
    can exercise a node without building one.
    """
    from langgraph.config import get_config

    try:
        configurable = (get_config() or {}).get("configurable") or {}
    except Exception:  # noqa: BLE001 - outside a graph, e.g. a unit test
        return ""
    return str(configurable.get("run_id") or "")


__all__ = [
    "BaseNode",
    "EmitFn",
    "LogFn",
    "NodeContext",
    "current_run_id",
    "make_string_param",
    "render_template",
    "template_scope",
]
