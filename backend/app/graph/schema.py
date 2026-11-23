"""Core graph schema — the SINGLE SOURCE OF TRUTH.

Everything else in this project derives from these models:

* the REST API serializes them,
* the frontend mirrors them in ``frontend/src/types/workflow.ts``,
* the Inspector form is rendered *generically* from :class:`ParamSpec`,
* the compiler turns :class:`WorkflowGraph` into a LangGraph ``StateGraph``,
* the AI tool-calling surface edits them.

If you change a model here, update ``frontend/src/types/workflow.ts`` by hand.
That is the only place drift can enter, and **nothing checks it yet** — this
line used to name ``scripts/check-schema-drift.ps1``, which does not exist. The
spec for writing it is `docs/code/task-13-postgres.md` §13b; see also
`docs/current/limitations.md` §3.1. Until it exists, a field added here and not
there shows up as an Inspector control that never renders.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class PortType(str, Enum):
    """Data type carried by a handle.

    Types are enforced at connect time (``isValidConnection`` in the frontend,
    ``validate_graph`` in the backend) and colour-coded on the canvas. This is
    the Langflow approach and it removes an entire class of wiring mistakes.
    """

    ANY = "any"
    TEXT = "text"
    MESSAGE = "message"
    MESSAGES = "messages"
    LANGUAGE_MODEL = "language_model"
    TOOL = "tool"
    RETRIEVER = "retriever"
    MEMORY = "memory"
    JSON = "json"
    TABLE = "table"
    EMBEDDINGS = "embeddings"
    NUMBER = "number"
    BOOLEAN = "boolean"


#: Canvas colour per port type. Kept next to the enum so the frontend and the
#: API docs never disagree about what "lime" means.
PORT_COLORS: dict[PortType, str] = {
    PortType.ANY: "#94a3b8",
    PortType.TEXT: "#38bdf8",
    PortType.MESSAGE: "#818cf8",
    PortType.MESSAGES: "#6366f1",
    PortType.LANGUAGE_MODEL: "#e879f9",
    PortType.TOOL: "#2dd4bf",
    PortType.RETRIEVER: "#34d399",
    PortType.MEMORY: "#fb923c",
    PortType.JSON: "#f87171",
    PortType.TABLE: "#f472b6",
    PortType.EMBEDDINGS: "#4ade80",
    PortType.NUMBER: "#fbbf24",
    PortType.BOOLEAN: "#a3e635",
}

#: Types that may be connected to one another. Symmetric by construction;
#: ``ANY`` is the universal donor and recipient.
_PORT_COMPATIBILITY: dict[PortType, set[PortType]] = {
    PortType.ANY: set(PortType),
    PortType.TEXT: {PortType.ANY, PortType.TEXT, PortType.MESSAGE},
    PortType.MESSAGE: {PortType.ANY, PortType.MESSAGE, PortType.TEXT, PortType.MESSAGES},
    PortType.MESSAGES: {PortType.ANY, PortType.MESSAGES, PortType.MESSAGE},
    PortType.LANGUAGE_MODEL: {PortType.ANY, PortType.LANGUAGE_MODEL},
    PortType.TOOL: {PortType.ANY, PortType.TOOL},
    PortType.RETRIEVER: {PortType.ANY, PortType.RETRIEVER},
    PortType.MEMORY: {PortType.ANY, PortType.MEMORY},
    PortType.JSON: {PortType.ANY, PortType.JSON, PortType.TABLE},
    PortType.TABLE: {PortType.ANY, PortType.TABLE, PortType.JSON},
    PortType.EMBEDDINGS: {PortType.ANY, PortType.EMBEDDINGS},
    PortType.NUMBER: {PortType.ANY, PortType.NUMBER, PortType.TEXT},
    PortType.BOOLEAN: {PortType.ANY, PortType.BOOLEAN, PortType.TEXT},
}


def ports_compatible(source: PortType, target: PortType) -> bool:
    """Return True when ``source`` may feed ``target``."""
    return target in _PORT_COMPATIBILITY.get(source, set()) or source is PortType.ANY


class InputPort(BaseModel):
    """A handle that accepts an incoming edge."""

    id: str = Field(description="Handle id; referenced by EdgeSpec.targetHandle.")
    label: str
    type: PortType = PortType.ANY
    required: bool = False
    description: str = ""
    #: Chinese override. Empty falls back to the English source above, so an
    #: untranslated node type still renders correctly.
    label_zh: str = ""
    description_zh: str = ""


class OutputPort(BaseModel):
    """A handle that emits an outgoing edge."""

    id: str = Field(description="Handle id; referenced by EdgeSpec.sourceHandle.")
    label: str
    type: PortType = PortType.ANY
    description: str = ""
    label_zh: str = ""
    description_zh: str = ""


# ---------------------------------------------------------------------------
# Parameters (drive the generic Inspector form)
# ---------------------------------------------------------------------------


class _ParamBase(BaseModel):
    """Common metadata every parameter control understands."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="Key inside NodeSpec.config.")
    label: str
    description: str = ""
    #: Chinese override; see InputPort for why the fallback is empty-string based.
    label_zh: str = ""
    description_zh: str = ""
    required: bool = False
    advanced: bool = Field(
        default=False,
        description="Hide behind an 'Advanced' disclosure to keep the panel calm.",
    )
    #: Show this field only when another config key equals one of these values.
    #: e.g. ``show_if={"field": "mode", "equals": ["http"]}``
    show_if: dict[str, Any] | None = None


class StringParam(_ParamBase):
    type: Literal["string"] = "string"
    default: str = ""
    placeholder: str = ""
    multiline: bool = False
    rows: int = 4
    #: Render as a password box and never echo the value back in run logs.
    secret: bool = False
    #: When set, render a dropdown instead of a free-text box.
    enum: list[str] | None = None


class NumberParam(_ParamBase):
    type: Literal["number"] = "number"
    default: float = 0
    min: float | None = None
    max: float | None = None
    step: float = 1
    integer: bool = False


class BooleanParam(_ParamBase):
    type: Literal["boolean"] = "boolean"
    default: bool = False


class SelectOption(BaseModel):
    label: str
    value: str
    description: str = ""
    label_zh: str = ""
    description_zh: str = ""


class SelectParam(_ParamBase):
    type: Literal["select"] = "select"
    default: str = ""
    options: list[SelectOption] = Field(default_factory=list)


class JsonParam(_ParamBase):
    """Free-form structured value, edited as JSON."""

    type: Literal["json"] = "json"
    default: Any = None
    rows: int = 6


class CodeParam(_ParamBase):
    """Source code, edited in a monospace editor."""

    type: Literal["code"] = "code"
    language: Literal["python", "javascript", "jinja2"] = "python"
    default: str = ""
    rows: int = 12


class ModelParam(_ParamBase):
    """A model identifier with an optional per-node override of the global config.

    Rendered as a compact model picker; the value is a dict so a node can pin
    its own ``base_url`` without touching global settings.
    """

    type: Literal["model"] = "model"
    default: dict[str, Any] = Field(default_factory=dict)


ParamSpec = Annotated[
    Union[
        StringParam,
        NumberParam,
        BooleanParam,
        SelectParam,
        JsonParam,
        CodeParam,
        ModelParam,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Node *type* definition (what the palette offers)
# ---------------------------------------------------------------------------


class NodeCategory(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    MODEL = "model"
    LOGIC = "logic"
    DATA = "data"
    INTEGRATION = "integration"
    CONTROL = "control"
    HUMAN = "human"
    CUSTOM = "custom"


class DynamicOutputs(str, Enum):
    """How a node's output handles are computed."""

    NONE = "none"
    """Fixed handles from :attr:`NodeTypeSpec.outputs`."""

    FROM_PARAM = "from_param"
    """One output handle per entry in a list-typed param (e.g. Condition branches)."""


class NodeTypeSpec(BaseModel):
    """Declarative definition of one node type.

    The frontend palette, the Inspector form, and the compiler all read this.
    Adding a node type therefore means writing **one** Python module and
    registering it — no frontend change required.
    """

    type: str = Field(description="Stable identifier, e.g. 'llm'.")
    label: str
    category: NodeCategory
    description: str = ""
    label_zh: str = ""
    description_zh: str = ""
    icon: str = Field(default="box", description="Lucide icon name.")
    color: str = "#64748b"
    inputs: list[InputPort] = Field(default_factory=list)
    outputs: list[OutputPort] = Field(default_factory=list)
    params: list[ParamSpec] = Field(default_factory=list)
    dynamic_outputs: DynamicOutputs = DynamicOutputs.NONE
    #: Config key holding the list that drives dynamic outputs.
    dynamic_outputs_param: str | None = None
    #: Exactly one node type should be the entry point; several may be terminal.
    is_entry: bool = False
    is_terminal: bool = False
    #: A group node is a visual container for sub-flows, not an executable step.
    is_container: bool = False
    #: Capability flags surfaced in the UI.
    supports_streaming: bool = False
    requires_trust: bool = Field(
        default=False,
        description="Executes arbitrary user code; the node must be marked trusted.",
    )
    #: What the *authoring model* needs to know that the ports and parameters do
    #: not say. Rendered into the graph-generation prompt; never shown to a user.
    #:
    #: It lives on the type rather than in `ai_buildtime/prompts.py` for the same
    #: reason the ports do: adding a node type is meant to be a one-file change,
    #: and a table of per-type advice kept somewhere else would be a second place
    #: to update — which is exactly the drift this schema is the single source of
    #: truth to prevent. A type with nothing surprising to say leaves it empty.
    #:
    #: English only, deliberately. The model reads English regardless of the
    #: interface language, and the same reasoning that keeps `ToolDef.description`
    #: untranslated for the model applies here (`app/tools/registry.py`).
    ai_notes: str = Field(
        default="",
        description="Runtime behaviour the authoring model must know. Prompt-only.",
    )


# ---------------------------------------------------------------------------
# Localisation
# ---------------------------------------------------------------------------

#: Languages the catalogue can be rendered in. English is the source text, so
#: it needs no overrides and can never be incomplete.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh")


#: Fields :func:`localize` will swap for their ``*_zh`` sibling. Any key not
#: listed here keeps its English value in every language — deliberately, because
#: some strings are *data* rather than chrome (a Condition's branch labels, a
#: tool's JSON Schema that the model itself reads).
LOCALIZABLE_KEYS: frozenset[str] = frozenset(
    {"label", "description", "message", "disabled_reason"}
)


def normalize_language(value: str | None) -> str:
    """Map an HTTP language tag onto a supported language.

    Accepts anything ``Accept-Language`` might carry (``zh-CN``, ``zh-Hans``)
    so a browser default works without configuration, and falls back to English
    rather than raising.
    """
    if not value:
        return "en"
    tag = value.strip().lower().replace("_", "-")
    base = tag.split("-")[0]
    return base if base in SUPPORTED_LANGUAGES else "en"


def localize(value: Any, lang: str) -> Any:
    """Render a specification tree in ``lang``.

    Translation happens on the *way out* of the API rather than in the UI, and
    that placement is the whole point: the frontend's palette, Inspector, and
    node cards keep reading a plain ``label``/``description``, so exactly one
    place decides the language and no component has to know about locales.

    The ``*_zh`` fields are stripped from the result — once resolved they are
    implementation detail, and shipping them would double every catalogue
    payload on every language.
    """
    if isinstance(value, list):
        return [localize(item, lang) for item in value]

    if not isinstance(value, dict):
        return value

    translated = lang != "en"
    result: dict[str, Any] = {}

    for key, item in value.items():
        if key.endswith("_zh"):
            continue
        if translated and key in LOCALIZABLE_KEYS:
            override = value.get(f"{key}_zh")
            # An empty override means "not translated yet", not "blank".
            if isinstance(override, str) and override:
                result[key] = override
                continue
        result[key] = localize(item, lang)

    return result


# ---------------------------------------------------------------------------
# Graph instance (what the user actually builds)
# ---------------------------------------------------------------------------


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeUI(BaseModel):
    """Purely visual state, persisted so the canvas round-trips faithfully."""

    parent_id: str | None = Field(
        default=None,
        description="React Flow parentId — used to nest a node inside a group/sub-flow.",
    )
    width: float | None = None
    height: float | None = None
    collapsed: bool = False
    #: Free-form canvas note rendered as a sticky. Not executed.
    note: str = ""


class NodeSpec(BaseModel):
    """One node on the canvas."""

    id: str
    type: str
    label: str = ""
    description: str = Field(
        default="",
        description=(
            "**The user's own words describing what this step should do.** "
            "Rendered on the node card and fed to the AI when filling in config."
        ),
    )
    #: Coordinates are **not** part of what the graph does, and `definition_digest`
    #: must not see them: `model_dump(exclude={"position"})` cannot reach a nested
    #: field, so the hash included them and dragging a node made every graph
    #: referencing that sub-workflow report "edited since you pinned it" and refuse
    #: to run. Marking the field `exclude=True` fixes the hash and breaks the canvas
    #: (measured: the key disappears from every dump, so the UI loses every node's
    #: position), so the digest builds its own payload instead — see
    #: `definition_digest`.
    position: Position = Field(default_factory=Position)
    config: dict[str, Any] = Field(default_factory=dict)
    ui: NodeUI = Field(default_factory=NodeUI)
    trusted: bool = Field(
        default=False,
        description="Required for node types with requires_trust (Code nodes).",
    )


class EdgeSpec(BaseModel):
    """One connection. ``sourceHandle``/``targetHandle`` matter because nodes
    may have several typed ports."""

    id: str
    source: str
    source_handle: str | None = Field(default=None, alias="sourceHandle")
    target: str
    target_handle: str | None = Field(default=None, alias="targetHandle")
    label: str = ""
    #: Rendered edge style; purely cosmetic.
    type: str = "smoothstep"

    model_config = ConfigDict(populate_by_name=True)


class Viewport(BaseModel):
    x: float = 0
    y: float = 0
    zoom: float = 1


class StateFieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    JSON = "json"
    LIST = "list"
    MESSAGES = "messages"


class ReducerType(str, Enum):
    """How a node's update to a shared-state key is merged (LangGraph reducers)."""

    OVERWRITE = "overwrite"
    """Default reducer: the right-hand (new) value wins."""

    APPEND = "append"
    """``operator.add`` — useful for accumulating lists."""

    ADD_MESSAGES = "add_messages"
    """Message-aware append that replaces by id and deserializes dicts."""

    MERGE_DICT = "merge_dict"
    """Shallow-merge dicts, so parallel branches can each add their own key."""


class StateField(BaseModel):
    """One channel of the graph's shared state.

    Shared state is the *primary* data channel, not an afterthought: edges alone
    cannot express cross-branch, non-adjacent, or loop-accumulated values.
    """

    name: str
    type: StateFieldType = StateFieldType.JSON
    reducer: ReducerType = ReducerType.OVERWRITE
    default: Any = None
    description: str = ""


class WorkflowGraph(BaseModel):
    """A complete workflow definition — exactly what gets persisted and run."""

    #: Constrained because the id becomes a *filename* (`data/workflows/<id>.json`
    #: — see `db.repository.export_workflow`). An unconstrained string let an
    #: imported payload carry `id="../../../../tmp/pwn"` and write outside the
    #: data directory. The character class is what the UI and tests already
    #: generate (`uuid4().hex[:12]`, `smoke-1789023655`, `wf-test`), so this
    #: tightens the contract without changing any legitimate value.
    id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:12],
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        description="Identifier; safe to use as a filename component.",
    )
    name: str = "Untitled workflow"
    description: str = ""
    version: int = 1
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)
    viewport: Viewport = Field(default_factory=Viewport)
    state_schema: list[StateField] = Field(
        default_factory=lambda: [
            StateField(
                name="vars",
                type=StateFieldType.JSON,
                reducer=ReducerType.MERGE_DICT,
                default={},
                description="Every node writes its output here under its own node id.",
            )
        ]
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    #: This workflow's own tool modules, keyed by module name (without `.py`).
    #:
    #: In the definition rather than on disk beside it, for the reason
    #: `db/models.py` gives about the graph itself: a workflow is always read and
    #: written as a unit, and splitting it would mean two places can disagree about
    #: what an agent is. The exporter writes them out as `tools/*.py`.
    #:
    #: This is what makes a business agent possible at all: "query our order
    #: service" is a tool, and before this the canvas had nowhere to put one.
    tools: dict[str, str] = Field(default_factory=dict)

    # -- lookups ------------------------------------------------------------
    def node(self, node_id: str) -> NodeSpec | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def executable_nodes(self) -> list[NodeSpec]:
        """Nodes that actually run (excludes visual containers)."""
        return [n for n in self.nodes if n.type != "group"]

    def outgoing(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.source == node_id]

    def incoming(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.target == node_id]

    @field_validator("edges", mode="before")
    @classmethod
    def _default_edge_ids(cls, value: Any) -> Any:
        """Tolerate hand-written / AI-generated edges without ids."""
        if isinstance(value, list):
            for edge in value:
                if isinstance(edge, dict) and not edge.get("id"):
                    edge["id"] = (
                        f"e-{edge.get('source')}-{edge.get('sourceHandle') or 'out'}"
                        f"-{edge.get('target')}-{edge.get('targetHandle') or 'in'}"
                    )
        return value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """A single problem found by the validator.

    ``code`` is the stable machine-readable handle; ``message`` is the English
    sentence shown to the user. ``message_zh`` is its Chinese counterpart and is
    resolved on the way out by :func:`localize` — the same contract the node
    catalogue uses, so the canvas needs no per-issue translation table.

    Messages interpolate node labels and port names, which is why the two
    languages are stored side by side rather than rebuilt from a ``code``
    template at render time.
    """

    severity: Literal["error", "warning"]
    code: str
    message: str
    message_zh: str = ""
    node_id: str | None = None
    edge_id: str | None = None


class ValidationReport(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    PENDING = "pending"
    #: Accepted, but waiting for a free execution slot. See `runtime/queue.py`.
    #:
    #: A distinct status rather than "pending" because the two mean different
    #: things to a user: pending is "just created, starts any moment", queued is
    #: "the platform is at its concurrency limit and this is in line". Without it
    #: the UI shows a run that looks stuck for no reason.
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: Stopped early on purpose, with allowance still left. See
    #: `runtime/projection.py`.
    #:
    #: A distinct status because neither neighbour is honest about it. `succeeded`
    #: would make a truncated answer indistinguishable from a complete one, and
    #: `failed` would report a run that was *managed* as one that broke — which
    #: turns a deliberate policy into a bug report.
    STOPPED = "stopped"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class RunStepStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunEventType(str, Enum):
    """Typed SSE events. One vocabulary for the whole UI."""

    RUN_STARTED = "run_started"
    #: The run is waiting for a free execution slot. Carries its queue position.
    RUN_QUEUED = "run_queued"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    NODE_FAILED = "node_failed"
    NODE_OUTPUT_CHUNK = "node_output_chunk"
    STATE_UPDATED = "state_updated"
    INTERRUPT_RAISED = "interrupt_raised"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    LOG = "log"


class RunEvent(BaseModel):
    """Payload pushed over SSE.

    ``node_id`` and ``step`` let the canvas highlight nodes and the timeline
    render one row per step, so a single stream serves both views.
    """

    type: RunEventType
    run_id: str
    seq: int = 0
    node_id: str | None = None
    node_label: str | None = None
    step: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = Field(default_factory=dict)


class RunCreateRequest(BaseModel):
    workflow_id: str
    #: Seed values for the graph's shared state.
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: Per-node config overrides applied for this run only (Langflow "tweaks").
    tweaks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    recursion_limit: int | None = None


class NodeTestRequest(BaseModel):
    """Single-node debug: run one node with hand-supplied upstream input."""

    workflow_id: str
    node_id: str
    #: The node as the canvas currently has it. Preferred over the stored copy
    #: when present, because the node being edited is the one being tested: a
    #: node added a moment ago is not in the store yet, and a config edit that
    #: has not been saved would otherwise be silently ignored.
    node: dict[str, Any] | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    config_overrides: dict[str, Any] = Field(default_factory=dict)


class RunRead(BaseModel):
    id: str
    workflow_id: str
    status: RunStatus
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


__all__ = [
    "PORT_COLORS",
    "SUPPORTED_LANGUAGES",
    "BooleanParam",
    "CodeParam",
    "DynamicOutputs",
    "EdgeSpec",
    "InputPort",
    "JsonParam",
    "ModelParam",
    "NodeCategory",
    "NodeSpec",
    "NodeTestRequest",
    "NodeTypeSpec",
    "NodeUI",
    "NumberParam",
    "OutputPort",
    "ParamSpec",
    "PortType",
    "Position",
    "ReducerType",
    "RunCreateRequest",
    "RunEvent",
    "RunEventType",
    "RunRead",
    "RunStatus",
    "RunStepStatus",
    "SelectOption",
    "SelectParam",
    "StateField",
    "StateFieldType",
    "StringParam",
    "ValidationIssue",
    "ValidationReport",
    "Viewport",
    "WorkflowGraph",
    "ports_compatible",
]
