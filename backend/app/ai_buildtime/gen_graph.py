"""Generate a whole workflow from a natural-language description.

How it works: the model is given a set of **graph-mutation tools** (add a node,
connect two nodes, configure a node, finish) and applies them one at a time
against a draft held in memory. This is the approach n8n's AI Workflow Builder
and the Langflow Assistant both take, and it is worth the extra plumbing over
"return the whole graph as JSON" for one reason: **each mutation can be
validated as it happens**. A model that invents a node type or wires an
impossible connection gets told so immediately, instead of producing a large
graph that fails validation at the end with no indication of where it went wrong.

The draft is returned to the client as a *preview*. Nothing touches the user's
canvas until they accept it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

from app.ai_buildtime.parsing import extract_json
from app.ai_buildtime.prompts import build_generate_graph_messages
from app.ai_buildtime.metering import record_reply_usage, reply_usage
from app.ai_buildtime.self_test import trial_feedback, trial_run
from app.runtime.ai.provider import build_chat_model, resolve_llm_config
from app.runtime.ai.messages import Language
from app.graph.layout import layout_nodes
from app.graph.schema import (
    EdgeSpec,
    NodeSpec,
    Position,
    StateField,
    WorkflowGraph,
    localize,
)
from app.graph.validate import validate_graph
from app.nodes import effective_outputs, get_node_type_spec, has_node_type, list_node_type_specs
from app.tools.registry import get_tool_registry


class GraphBuildError(RuntimeError):
    """Raised when a graph cannot be generated."""


#: How many times generation may hand the validator's complaints back to the
#: model before giving up and returning the graph as it stands.
#:
#: Three, not "until it passes". Each round costs a full model call over a
#: conversation that is already long, and the failures this loop exists to catch
#: — an unwired branch, a missing back edge — are either fixed in one round or
#: are a sign the model has lost the thread and will not recover by being told
#: again. A graph that is still invalid after three attempts is returned with
#: its validation report so the user sees the problem, which is the same outcome
#: as an unbounded loop with a much smaller bill.
_MAX_CORRECTIONS = 3

#: How many times a *runtime* failure may be handed back before the graph is
#: returned as it stands.
#:
#: Separate from `_MAX_CORRECTIONS` because the two failures are different:
#: validation mistakes are usually wiring the model can see in `inspect_canvas`,
#: while a trial-run failure names a node and an error the model has to reason
#: from. Two rounds is enough to fix a path or a shape; a third usually means the
#: design is wrong in a way another edit will not discover.
_MAX_TEST_CORRECTIONS = 2


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


class GraphDraft:
    """A workflow under construction, with validating mutation methods.

    Every mutator returns a short human-readable string. Those strings become the
    tool results the model sees, so they double as the model's feedback channel.
    """

    def __init__(self, existing: WorkflowGraph | None = None) -> None:
        self.nodes: list[NodeSpec] = []
        self.edges: list[EdgeSpec] = []
        self.state_schema: list[StateField] = (
            list(existing.state_schema) if existing else []
        )
        self.notes: list[str] = []
        #: A name the model proposed via `finish`. Empty until then; when set it
        #: wins over the caller's placeholder name (a fresh canvas is called
        #: "Untitled workflow" until somebody decides what it is).
        self.suggested_name: str | None = None
        #: Existing node ids, so an edit can tell the nodes it added from the
        #: ones the user already placed. `to_graph(preserve_positions=True)`
        #: uses this to leave the user's layout alone and only place new nodes.
        self._original_ids: set[str] = set()
        #: The document being edited. Its id must survive every streamed
        #: snapshot: the canvas keys the open workflow and its conversation by
        #: id, so a fresh id per snapshot looks like a document switch and wipes
        #: the transcript. Only a build from scratch mints an id.
        self._original_graph_id: str = existing.id if existing else ""
        #: Nodes this edit has already positioned. Placement depends on the
        #: neighbours' current positions, so re-running it for a node is not
        #: idempotent: the stagger offset in `_place_new_nodes` compounds on
        #: every streamed snapshot. A live build emits one snapshot per tool
        #: call, which was enough to push a fresh graph tens of thousands of
        #: pixels below the viewport — the canvas looked empty while the nodes
        #: were all still there. Place each node once, then leave it alone.
        self._placed_ids: set[str] = set()

        if existing:
            # Extending an existing canvas: carry its nodes over so ids stay
            # stable and the model can connect to what is already there.
            self.nodes = [node.model_copy(deep=True) for node in existing.nodes]
            self.edges = [edge.model_copy(deep=True) for edge in existing.edges]
            self._original_ids = {node.id for node in existing.nodes}

    # ------------------------------------------------------------- mutations
    def add_node(
        self,
        type: str,
        label: str,
        description: str = "",
        config: dict[str, Any] | None = None,
    ) -> str:
        if not has_node_type(type):
            available = ", ".join(sorted(s.type for s in list_node_type_specs()))
            return f"ERROR: unknown node type '{type}'. Available types: {available}"

        spec = get_node_type_spec(type)
        node_id = self._unique_id(type)

        merged = dict(config or {})
        # Apply declared defaults for anything the model left out, so a required
        # field with a sensible default is never reported as missing.
        from app.nodes import get_node_class

        for key, value in get_node_class(type).defaults_for(spec).items():
            merged.setdefault(key, value)

        self.nodes.append(
            NodeSpec(
                id=node_id,
                type=type,
                label=label or spec.label,
                description=description or "",
                config=merged,
                # Deliberately NOT `bool(spec.requires_trust)`.
                #
                # Trust is the one decision the AI must not make on the user's
                # behalf. A Code node runs arbitrary Python in-process, and
                # `requires_trust` exists precisely so a human looks at it before
                # it can run. Auto-trusting meant a graph generated from a
                # sentence could execute code the user never reviewed and never
                # ticked — the gate was decorative.
                #
                # Leaving this false makes `validate_graph` raise `untrusted_node`,
                # so a generated graph containing a Code node comes back visibly
                # *gated* (the Inspector shows the unticked box, the validator
                # says what to do) rather than silently armed.
                trusted=False,
            )
        )
        return (
            f"Added '{node_id}' ({type}, label '{label}'). "
            f"Inputs: {', '.join(p.id for p in spec.inputs) or 'none'}. "
            f"Outputs: {', '.join(p.id for p in spec.outputs) or 'none'}."
        )

    def update_node(
        self,
        node_id: str,
        config: dict[str, Any] | None = None,
        label: str | None = None,
        description: str | None = None,
    ) -> str:
        node = self._find(node_id)
        if node is None:
            return f"ERROR: no node with id '{node_id}'."

        if label:
            node.label = label
        if description:
            node.description = description

        if config:
            spec = get_node_type_spec(node.type)
            allowed = {param.name for param in spec.params}
            rejected = [key for key in config if key not in allowed]
            node.config.update({k: v for k, v in config.items() if k in allowed})
            if rejected:
                return (
                    f"Updated '{node_id}'. Ignored unknown field(s): "
                    f"{', '.join(rejected)}. Valid fields: {', '.join(sorted(allowed))}"
                )
        return f"Updated '{node_id}'."

    def connect(
        self,
        source: str,
        target: str,
        source_handle: str | None = None,
        target_handle: str | None = None,
        label: str = "",
    ) -> str:
        source_node = self._find(source)
        target_node = self._find(target)
        if source_node is None:
            return f"ERROR: no node with id '{source}'."
        if target_node is None:
            return f"ERROR: no node with id '{target}'."

        out_ports = effective_outputs(source_node)
        if source_handle is None:
            source_handle = out_ports[0].id if out_ports else None
        if source_handle and source_handle not in {p.id for p in out_ports}:
            return (
                f"ERROR: '{source}' has no output '{source_handle}'. "
                f"Available: {', '.join(p.id for p in out_ports) or 'none'}."
            )

        in_ports = get_node_type_spec(target_node.type).inputs
        if target_handle is None:
            target_handle = in_ports[0].id if in_ports else None
        if target_handle and target_handle not in {p.id for p in in_ports}:
            return (
                f"ERROR: '{target}' has no input '{target_handle}'. "
                f"Available: {', '.join(p.id for p in in_ports) or 'none'}."
            )

        if any(
            e.source == source
            and e.target == target
            and e.source_handle == source_handle
            and e.target_handle == target_handle
            for e in self.edges
        ):
            return f"That connection already exists ({source} -> {target})."

        self.edges.append(
            EdgeSpec(
                id=f"e{len(self.edges) + 1}-{source}-{target}",
                source=source,
                source_handle=source_handle,
                target=target,
                target_handle=target_handle,
                # A short purpose, in the user's language. The canvas keeps it
                # hidden until the edge is clicked, and the edge inspector shows
                # it under "标签"; without one, a dense graph's connections are
                # only readable by tracing node ids.
                label=(label or "").strip()[:120],
            )
        )
        return (
            f"Connected {source}.{source_handle} -> {target}.{target_handle}"
            + (f" labelled '{label.strip()[:120]}'." if label and label.strip() else ".")
        )

    def disconnect(
        self,
        source: str,
        target: str,
        source_handle: str | None = None,
        target_handle: str | None = None,
    ) -> str:
        """Remove an existing connection.

        Generation could only add edges; an edit that needs to rewire a branch
        (move the "no" path from one node to another) had no way to say "this
        line is wrong" except deleting a node and rebuilding it. That is a much
        larger change than the user asked for, and it loses the node's config.
        """
        matches = [
            edge
            for edge in self.edges
            if edge.source == source
            and edge.target == target
            and (source_handle is None or edge.source_handle == source_handle)
            and (target_handle is None or edge.target_handle == target_handle)
        ]
        if not matches:
            return (
                f"ERROR: no connection {source} -> {target}"
                + (f" from '{source_handle}'" if source_handle else "")
                + (f" to '{target_handle}'" if target_handle else "")
                + "."
            )

        removed = {edge.id for edge in matches}
        self.edges = [edge for edge in self.edges if edge.id not in removed]
        return f"Removed {len(matches)} connection(s) {source} -> {target}."

    def delete_node(self, node_id: str) -> str:
        node = self._find(node_id)
        if node is None:
            return f"ERROR: no node with id '{node_id}'."
        self.nodes = [n for n in self.nodes if n.id != node_id]
        self.edges = [
            e for e in self.edges if e.source != node_id and e.target != node_id
        ]
        return f"Deleted '{node_id}' and its connections."

    def set_state_schema(self, fields: list[dict[str, Any]]) -> str:
        parsed: list[StateField] = []
        for item in fields:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            try:
                parsed.append(StateField.model_validate(item))
            except Exception:  # noqa: BLE001 - report rather than crash
                return f"ERROR: invalid state field: {item}"
        self.state_schema = parsed
        return f"Set {len(parsed)} state field(s)."

    def get_canvas(self) -> str:
        import json

        return json.dumps(self.to_graph().model_dump(mode="json"), ensure_ascii=False)

    def inspect_canvas(self) -> str:
        """The graph as a flow listing, plus what is wrong with it.

        The tool to reach for while building. ``get_canvas`` returns the graph as
        the *API* sees it — coordinates, colours, every empty config — which is
        the right shape for a round trip and the wrong shape for reading: at
        thirty nodes the one line that matters is buried in ninety that do not.

        This reuses `app.graph.inspect.describe_graph`, so the authoring model
        and any future caller see the same rendering, and the diagnostics are
        testable without a model in the loop.
        """
        from app.graph.inspect import describe_graph

        return describe_graph(self.to_graph())

    def finish(self, summary: str = "", name: str = "") -> str:
        if summary:
            self.notes.append(summary)
        cleaned = " ".join((name or "").split())
        if cleaned:
            self.suggested_name = cleaned[:60]
        return "Workflow complete."

    # -------------------------------------------------------------- helpers
    def _find(self, node_id: str) -> NodeSpec | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def _unique_id(self, node_type: str) -> str:
        base = f"{node_type}_{uuid.uuid4().hex[:4]}"
        existing = {n.id for n in self.nodes}
        while base in existing:
            base = f"{node_type}_{uuid.uuid4().hex[:4]}"
        return base

    def to_graph(
        self, *, name: str | None = None, preserve_positions: bool = False
    ) -> WorkflowGraph:
        if preserve_positions and self._original_ids:
            self._place_new_nodes()
        else:
            # Nothing on the canvas to preserve: this is a build from scratch,
            # so use the same layered layout as whole-graph generation. The
            # incremental "near its neighbours" placement is for edits to a
            # hand-placed canvas; applied to a blank one it lined the nodes up
            # in an arbitrary order instead of by flow.
            self.layout()
        return WorkflowGraph(
            id=self._original_graph_id or uuid.uuid4().hex[:12],
            name=self.suggested_name or name or "Generated workflow",
            description=self.notes[-1] if self.notes else "",
            nodes=list(self.nodes),
            edges=list(self.edges),
            state_schema=self.state_schema
            or WorkflowGraph().state_schema,
        )

    def layout(self) -> None:
        layout_nodes(self.nodes, self.edges)

    def _place_new_nodes(self) -> None:
        """Place only the nodes this edit added, near their neighbours.

        An edit must not re-flow the user's canvas: their placement is the
        document. The new nodes still need somewhere to go, so anchor each one
        to the nodes it connects to — right of its inputs, left of its outputs —
        and fall back to the right edge of the graph when it has no neighbours
        yet.
        """
        if not self.nodes:
            return

        pending = [
            node
            for node in self.nodes
            if node.id not in self._original_ids and node.id not in self._placed_ids
        ]
        if not pending:
            return

        by_id = {node.id: node for node in self.nodes}
        incoming: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        outgoing: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            if edge.source in outgoing and edge.target in incoming:
                outgoing[edge.source].append(edge.target)
                incoming[edge.target].append(edge.source)

        # Anything already on the canvas — the user's nodes plus the new ones
        # placed by an earlier snapshot — is a safe anchor. Only these have a
        # meaningful position; a pending node not yet placed in this pass is
        # still at the default (0, 0).
        placed = {
            node.id
            for node in self.nodes
            if node.id in self._original_ids or node.id in self._placed_ids
        }
        right_edge = max(
            (node.position.x for node in self.nodes if node.id in placed),
            default=0,
        )

        for index, node in enumerate(pending):
            ins = [
                by_id[n].position
                for n in incoming.get(node.id, [])
                if n in placed and n in by_id
            ]
            outs = [
                by_id[n].position
                for n in outgoing.get(node.id, [])
                if n in placed and n in by_id
            ]

            if ins and outs:
                x = (max(p.x for p in ins) + min(p.x for p in outs)) // 2
                y = (sum(p.y for p in ins) + sum(p.y for p in outs)) // (len(ins) + len(outs))
            elif ins:
                x = max(p.x for p in ins) + 300
                y = sum(p.y for p in ins) // len(ins)
            elif outs:
                x = min(p.x for p in outs) - 300
                y = sum(p.y for p in outs) // len(outs)
            elif placed:
                x = right_edge + 340 * (index + 1)
                y = 0
            else:
                x = index * 340
                y = 0

            # New nodes can share a neighbour; stagger them so they do not
            # land exactly on top of each other.
            node.position = Position(x=x, y=y + index * 40)
            placed.add(node.id)
            self._placed_ids.add(node.id)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

def _node_type_list() -> str:
    """Valid node types for `add_node`, filtered for the public demo."""
    from app.settings import get_settings

    specs = list_node_type_specs()
    if get_settings().public_demo:
        specs = [spec for spec in specs if spec.type != "code"]
    return ", ".join(sorted(spec.type for spec in specs))


def _tool_list() -> str:
    """Tool names for the prompt, read when the prompt is built.

    Not a module constant: the registry is a capability the deployment
    configures (`app.tools.registry`), so a list captured at import could offer
    the model a tool this agent does not have — and the model would then
    generate a graph that fails at run time.
    """
    return ", ".join(tool.name for tool in get_tool_registry().list())


def draft_tools() -> list[dict[str, Any]]:
    """OpenAI function schemas for the graph-mutation tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "add_node",
                "description": (
                    f"Add a node to the canvas. Valid types: {_node_type_list()}. "
                    f"Available tools for the 'tool' node type: {_tool_list()}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "description": "Node type id."},
                        "label": {
                            "type": "string",
                            "description": "Short name shown on the node card.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One sentence, in the user's language, explaining what "
                                "this step does. This is shown on the canvas."
                            ),
                        },
                        "config": {
                            "type": "object",
                            "description": "Initial configuration for the node.",
                        },
                    },
                    "required": ["type", "label", "description"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_node",
                "description": "Change an existing node's config, label, or description.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "config": {"type": "object"},
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["node_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "connect",
                "description": (
                    "Connect one node's output to another node's input. For a "
                    "condition node, pass the branch id as source_handle. Always "
                    "write a short label saying what this connection carries."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source node id."},
                        "target": {"type": "string", "description": "Target node id."},
                        "source_handle": {
                            "type": "string",
                            "description": "Output handle id. Defaults to the first.",
                        },
                        "target_handle": {
                            "type": "string",
                            "description": "Input handle id. Defaults to the first.",
                        },
                        "label": {
                            "type": "string",
                            "description": (
                                "Short purpose of this connection in the user's "
                                "language, e.g. '检索式' or '审批通过后写库'. "
                                "Shown when the edge is clicked; required."
                            ),
                        },
                    },
                    "required": ["source", "target", "label"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "disconnect",
                "description": (
                    "Remove an existing connection. Use this to rewire a branch "
                    "instead of deleting and rebuilding the node. Omit the "
                    "handles to remove every connection between the two nodes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source node id."},
                        "target": {"type": "string", "description": "Target node id."},
                        "source_handle": {"type": "string"},
                        "target_handle": {"type": "string"},
                    },
                    "required": ["source", "target"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_node",
                "description": "Remove a node and all its connections.",
                "parameters": {
                    "type": "object",
                    "properties": {"node_id": {"type": "string"}},
                    "required": ["node_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_canvas",
                "description": (
                    "Read the canvas as a flow listing: every node, what follows "
                    "it, the problems the validator would raise, and the shapes "
                    "that are legal but probably not intended (dead ends, "
                    "branches wired to nothing, a loop with no way back, an End "
                    "node reading a side effect). Use this while building and "
                    "before calling finish. Prefer it over get_canvas unless you "
                    "need the raw JSON."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_canvas",
                "description": (
                    "Read the current canvas as raw JSON, exactly as it is "
                    "stored. Verbose; use inspect_canvas for reading."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Call when the workflow is complete and coherent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "One sentence describing the finished workflow.",
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "A short, specific title for the workflow, 2–8 words "
                                "(for example 'Memory market intelligence'). Pass it "
                                "when building a new workflow or when the user asks "
                                "to rename one; omit it when editing an already-named "
                                "workflow."
                            ),
                        },
                    },
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Generation loop
# ---------------------------------------------------------------------------


#: Tools whose call changes the draft. Only these produce a `graph` event; the
#: read-only tools would send an identical snapshot and make the live preview
#: flicker for nothing.
_MUTATING_TOOLS = {
    "add_node",
    "update_node",
    "connect",
    "disconnect",
    "delete_node",
    "set_state_schema",
}


async def generate_graph(
    prompt: str,
    *,
    existing: WorkflowGraph | None = None,
    max_iterations: int | None = None,
    lang: Language = "en",
    self_test: bool | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Build a workflow from ``prompt`` and return it for review.

    ``on_event`` receives progress as it happens so a client can draw the draft
    while it is being built. The events are deliberately *snapshots*, not
    mutations: the client renders the graph it receives and never replays the
    model's edits itself, which keeps one source of truth for layout and ids.
    """
    from app.ai_buildtime.gen_node import AINotConfigured
    from app.settings import get_settings

    settings = get_settings()
    if not settings.ai_enabled:
        raise AINotConfigured("AI features are disabled. Set AI_ENABLED=true in .env.")

    budget = max_iterations or settings.ai_max_tool_iterations
    run_self_test = settings.ai_self_test if self_test is None else self_test

    draft = GraphDraft(existing)
    model = build_chat_model({"temperature": 0.0}, settings=settings, streaming=False)
    model_name = getattr(model, "model_name", "")
    if model_name == "mock-model":
        raise AINotConfigured(
            "No LLM API key is configured, so workflow generation is unavailable. "
            "Set LLM_API_KEY in .env and restart the backend."
        )

    tools = draft_tools()
    model = model.bind_tools(tools)

    messages: list[Any] = build_generate_graph_messages(prompt, existing)
    transcript: list[dict[str, Any]] = []
    finished = False
    corrections = 0
    test_corrections = 0
    test_result: dict[str, Any] | None = None
    graph_name = _name_from(prompt)

    async def publish(event: dict[str, Any]) -> None:
        if on_event is not None:
            await on_event(event)

    usage_totals: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }

    async def note_usage(usage: Any) -> None:
        for key in usage_totals:
            usage_totals[key] += int(getattr(usage, key, 0) or 0)
        await publish(
            {
                "type": "usage",
                "usage": {
                    "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
                    "calls": int(getattr(usage, "calls", 1) or 1),
                },
                "total": dict(usage_totals),
            }
        )

    previous = draft.to_graph(name=graph_name).model_dump(mode="json")
    await publish(
        {
            "type": "status",
            "phase": "building",
            "message": "Building the workflow.",
            "model": model_name,
        }
    )

    # `+ _MAX_CORRECTIONS + ...` so a correction round can never be squeezed out
    # by a build that used the whole budget. The failure that prevents is
    # specific and bad: a forty-node graph spends every round building, reaches
    # `finish` with one unwired branch, and is returned unfixed — because the
    # loop that exists to catch exactly that had no turns left. Corrections are
    # bounded anyway, so reserving room for them costs nothing on a graph that
    # does not need them.
    #
    # Test corrections get four turns each because a runtime fix can need
    # `inspect_canvas`, an `update_node` and a `finish` in separate rounds.
    turns = budget + _MAX_CORRECTIONS + _MAX_TEST_CORRECTIONS * 4
    for _step in range(turns):
        response = await model.ainvoke(messages)
        record_reply_usage(response)
        usage = reply_usage(response)
        if usage is not None:
            await note_usage(usage)
        messages.append(response)

        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            # The model stopped calling tools. Treat that as done if it built
            # something, rather than looping until the budget runs out.
            if draft.nodes:
                finished = True
            break

        for call in calls:
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            if isinstance(args, str):
                args = extract_json(args) or {}

            await publish({"type": "tool", "tool": name, "arguments": _brief(args)})
            result = _dispatch(draft, name, args)
            transcript.append({"tool": name, "arguments": args, "result": result})
            await publish(
                {
                    "type": "tool_result",
                    "tool": name,
                    "ok": not str(result).startswith("ERROR"),
                    "result": str(result)[:800],
                }
            )

            if name in _MUTATING_TOOLS:
                snapshot = draft.to_graph(name=graph_name).model_dump(mode="json")
                await publish(
                    {
                        "type": "graph",
                        "graph": snapshot,
                        "changed": _graph_diff(previous, snapshot),
                    }
                )
                previous = snapshot

            from langchain_core.messages import ToolMessage

            messages.append(
                ToolMessage(
                    content=str(result)[:4000],
                    tool_call_id=str(call.get("id") or f"call_{name}"),
                )
            )

            if name == "finish":
                finished = True

        if not finished:
            continue

        # The model says it is done. Whether it *is* done is a question about
        # the graph, not about its confidence — so ask the validator, and hand
        # back what it found.
        #
        # This is the difference between a generator and an author. A model
        # building forty nodes makes wiring mistakes it cannot see: a branch
        # wired to nothing, a loop whose back edge is missing, an End node
        # reading a side effect. Each is legal, each compiles, and each is
        # obvious the moment the run fails — which, without this loop, is the
        # first moment anyone finds out. `repair_graph` already worked this way;
        # generation simply never did.
        report = validate_graph(draft.to_graph(name=graph_name))
        blocking = report.errors
        await publish(
            {
                "type": "validation",
                "ok": not blocking,
                "errors": [
                    {"code": issue.code, "node_id": issue.node_id, "message": issue.message}
                    for issue in blocking
                ],
                "corrections": corrections,
            }
        )

        if blocking:
            if corrections >= _MAX_CORRECTIONS:
                break

            corrections += 1
            finished = False  # it is not finished until it would run
            feedback = _correction_message(blocking)
            transcript.append(
                {
                    "tool": "validate",
                    "arguments": {},
                    "result": f"{len(blocking)} error(s); asked the model to fix them.",
                }
            )

            from langchain_core.messages import HumanMessage

            messages.append(HumanMessage(content=feedback))
            continue

        # Valid — which says it would compile, not that it would do its job. A
        # trial run on the Start node's own defaults is the only check that can
        # catch an unset input, an empty render or a wrong-shaped value, and it
        # runs here, before the user ever sees the draft.
        if run_self_test:
            await publish({"type": "self_test", "phase": "start"})
            test_result = await trial_run(draft.to_graph(name=graph_name))
            transcript.append(
                {
                    "tool": "self_test",
                    "arguments": {},
                    "result": test_result["summary"],
                    "status": test_result["status"],
                }
            )
            await publish(
                {
                    "type": "self_test",
                    "phase": "done",
                    "result": _public_test(test_result),
                }
            )

            if (
                test_result["status"] == "failed"
                and not test_result["environment"]
                and test_corrections < _MAX_TEST_CORRECTIONS
            ):
                test_corrections += 1
                finished = False
                await publish(
                    {
                        "type": "correcting",
                        "kind": "runtime",
                        "corrections": test_corrections,
                        "failed_node": test_result.get("failed_node"),
                        "message": test_result.get("error"),
                    }
                )
                from langchain_core.messages import HumanMessage

                messages.append(HumanMessage(content=trial_feedback(test_result)))
                continue

        break

    if not draft.nodes:
        raise GraphBuildError(
            "The model did not build any nodes. Try describing the workflow in more "
            "detail, or check that the configured model supports tool calling."
        )

    graph = draft.to_graph(name=graph_name)
    report = validate_graph(graph)

    return {
        "graph": graph.model_dump(mode="json"),
        # Localised like `repair_graph` does it: the issue messages name fields and
        # ports the user sees, and those are labelled in their language. An
        # English sentence quoting a Chinese control points at nothing on screen.
        "validation": localize(report.model_dump(mode="json"), lang),
        "transcript": transcript,
        "finished": finished,
        # Surfaced so the dialog can say "checked and corrected twice" rather
        # than presenting a self-repaired graph as a first-try success. Whether
        # the model needed correcting is useful to the person deciding how much
        # to trust the result.
        "corrections": corrections,
        # The trial run, or `None` when it was disabled or never reached because
        # the graph still has validation errors. Keeping its full report (not
        # just a boolean) lets the dialog say *which node* failed and lets the
        # user judge the draft before accepting it.
        "test": test_result,
        "test_corrections": test_corrections,
        "self_test": run_self_test,
        "usage": dict(usage_totals),
        "summary": draft.notes[-1] if draft.notes else "",
    }


def _graph_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    """Which nodes and edges the latest mutation changed.

    Compared field-by-field on the parts a reader can see rather than on the
    whole snapshot, because `layout()` re-assigns every position whenever a node
    is added — comparing position would report the entire canvas as "changed" on
    every add and the live preview's highlight would be meaningless.
    """
    node_fields = ("type", "label", "description", "config")
    edge_fields = ("source", "source_handle", "target", "target_handle", "label")

    before_nodes = {n["id"]: n for n in before.get("nodes", [])}
    after_nodes = {n["id"]: n for n in after.get("nodes", [])}
    before_edges = {e["id"]: e for e in before.get("edges", [])}
    after_edges = {e["id"]: e for e in after.get("edges", [])}

    return {
        "added_nodes": [i for i in after_nodes if i not in before_nodes],
        "removed_nodes": [i for i in before_nodes if i not in after_nodes],
        "updated_nodes": [
            i
            for i in after_nodes
            if i in before_nodes
            and any(after_nodes[i].get(f) != before_nodes[i].get(f) for f in node_fields)
        ],
        "added_edges": [i for i in after_edges if i not in before_edges],
        "removed_edges": [i for i in before_edges if i not in after_edges],
        "updated_edges": [
            i
            for i in after_edges
            if i in before_edges
            and any(after_edges[i].get(f) != before_edges[i].get(f) for f in edge_fields)
        ],
    }


def _public_test(result: dict[str, Any]) -> dict[str, Any]:
    """The part of a trial result worth pushing over the stream."""
    return {
        key: result.get(key)
        for key in (
            "status",
            "ok",
            "summary",
            "error",
            "failed_node",
            "failed_node_label",
            "duration_ms",
            "environment",
            "outputs",
            "trace",
        )
    }


def _brief(args: dict[str, Any], limit: int = 400) -> dict[str, Any]:
    """Tool arguments for the live status line, with long strings clipped.

    A Code node's `config.code` can be thousands of characters; sending it over
    the wire so a client can print "add_node" beside it would make every
    mutation event larger than the graph it describes.
    """
    brief: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > limit:
            brief[key] = value[:limit] + "…"
        elif isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
            brief[key] = value if len(text) <= limit else text[:limit] + "…"
        else:
            brief[key] = value
    return brief


def _correction_message(errors: list[Any]) -> str:
    """The validator's complaints, phrased as work to do.

    Deliberately not the same text the user sees. The user's copy explains what
    is wrong; this one has to also say what to do about it and — more
    importantly — what *not* to do. A model told "3 problems remain" will
    otherwise delete the node that is causing one, which passes validation and
    destroys the user's workflow. The repair prompt already carries that rule;
    this loop needs it too, and stating it here keeps the two independent.
    """
    lines = [
        "The workflow does not pass validation yet, so it would not run. "
        f"{len(errors)} problem(s) remain:",
        "",
    ]
    for index, issue in enumerate(errors, start=1):
        where = f" (node {issue.node_id})" if issue.node_id else ""
        lines.append(f"{index}. [{issue.code}]{where} {issue.message}")

    lines += [
        "",
        "Fix these with the tools, then call `finish` again.",
        "Rules for this repair:",
        "- Fix only what is listed. Do not redesign, re-order or 'improve' "
        "anything else — a change the user cannot see coming is worse than the "
        "bug.",
        "- Do not delete a node to silence a complaint. If a problem cannot be "
        "solved without removing something, leave it and say so in the summary.",
        "- Keep every existing node id stable; connections depend on them.",
        "- Call `inspect_canvas` if you need to see the current wiring before "
        "editing.",
    ]
    return "\n".join(lines)


def _dispatch(draft: GraphDraft, name: str, args: dict[str, Any]) -> str:
    """Route one tool call. Never raises — the error text is the feedback."""
    handlers = {
        "add_node": draft.add_node,
        "update_node": draft.update_node,
        "connect": draft.connect,
        "disconnect": draft.disconnect,
        "delete_node": draft.delete_node,
        "set_state_schema": draft.set_state_schema,
        "inspect_canvas": draft.inspect_canvas,
        "get_canvas": draft.get_canvas,
        "finish": draft.finish,
    }
    handler = handlers.get(name)
    if handler is None:
        return f"ERROR: unknown tool '{name}'."

    try:
        # Filter to the handler's real parameters so a model adding an extra key
        # does not turn into a TypeError.
        import inspect

        allowed = set(inspect.signature(handler).parameters)
        kwargs = {k: v for k, v in (args or {}).items() if k in allowed}
        return str(handler(**kwargs))
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"


def _name_from(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if len(cleaned) <= 60:
        return cleaned or "Generated workflow"
    return cleaned[:57].rstrip() + "…"


__all__ = ["GraphBuildError", "GraphDraft", "draft_tools", "generate_graph"]
