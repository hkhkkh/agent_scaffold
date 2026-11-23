"""Node type registry.

Node modules call :func:`register_node` at import time. ``app.nodes`` imports
every built-in module, so a plain ``import app.nodes`` populates the registry.

Adding a node type is a single-file change:
1. Write ``app/nodes/my_node.py`` with a :class:`NodeTypeSpec` + ``BaseNode``.
2. ``@register_node`` it.
3. Import it in ``app/nodes/__init__.py``.

The palette, Inspector form, validator, compiler, and AI prompts all pick it up
automatically — no frontend edit required.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from app.graph.schema import (
    DynamicOutputs,
    JsonParam,
    NodeSpec,
    NodeTypeSpec,
    OutputPort,
    ParamSpec,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.runtime.attempts import ON_ERROR_CONTINUE, ON_ERROR_DEFAULT, ON_ERROR_STOP
from app.settings import Settings

from app.nodes.base import BaseNode

#: Shown in the timeout parameter's description. Read from Settings rather than
#: written as a number, so the text cannot drift from the value it describes.
_DEFAULT_TIMEOUT: float = Settings.model_fields["node_timeout_seconds"].default

_REGISTRY: dict[str, type[BaseNode]] = {}
_ORDER: list[str] = []


class NodeTypeError(KeyError):
    """Raised when a graph references an unknown node type."""


def register_node(cls: type[BaseNode]) -> type[BaseNode]:
    """Class decorator that adds a node type to the registry."""
    spec = getattr(cls, "spec", None)
    if spec is None:
        raise ValueError(f"{cls.__name__} must define a class-level `spec`.")
    if spec.type in _REGISTRY and _REGISTRY[spec.type] is not cls:
        raise ValueError(f"Duplicate node type registration: '{spec.type}'")
    _REGISTRY[spec.type] = cls
    if spec.type not in _ORDER:
        _ORDER.append(spec.type)
    return cls


def has_node_type(node_type: str) -> bool:
    return node_type in _REGISTRY


def get_node_class(node_type: str) -> type[BaseNode]:
    try:
        return _REGISTRY[node_type]
    except KeyError as exc:  # pragma: no cover - defensive
        raise NodeTypeError(
            f"Unknown node type '{node_type}'. Registered: {sorted(_REGISTRY)}"
        ) from exc


def get_node_type_spec(node_type: str) -> NodeTypeSpec:
    return get_node_class(node_type).spec


def list_node_type_specs() -> list[NodeTypeSpec]:
    """All registered types, ordered by category then label for the palette."""
    specs = [get_node_class(t).spec for t in _ORDER]
    return sorted(specs, key=lambda s: (s.category.value, s.label))


# ---------------------------------------------------------------------------
# Specs that depend on runtime state
# ---------------------------------------------------------------------------

#: Builders for node types whose spec cannot be a module constant.
#:
#: The Tool node is the only member today: its dropdown is the tool registry's
#: contents, and that registry is a capability the deployment configures
#: (`app.tools.registry`). A spec frozen at import would offer a user tools this
#: deployment does not have — they would configure one, save, and only discover
#: at run time that it is unavailable.
#:
#: A refresher rebuilds the whole spec and the class attribute is replaced, so
#: every existing consumer (``list_node_type_specs``, ``get_node_type_spec``, the
#: validator, the AI prompts) sees current data without changing.
_SPEC_REFRESHERS: dict[str, Callable[[], NodeTypeSpec]] = {}


def register_spec_refresher(
    node_type: str, builder: Callable[[], NodeTypeSpec]
) -> None:
    """Declare how to rebuild ``node_type``'s spec from current state."""
    _SPEC_REFRESHERS[node_type] = builder


def refresh_dynamic_specs() -> list[str]:
    """Re-derive every spec that depends on runtime state. Returns their types.

    Called from the places that *render* a catalogue or build a prompt, not from
    the getters. A getter with a side effect is a trap for the next reader, and
    the cost of being explicit here is one call in two places.

    Refreshing is cheap and idempotent; it is also skipped for a node type that
    was never registered, so this cannot resurrect a removed node.
    """
    refreshed: list[str] = []
    for node_type, builder in _SPEC_REFRESHERS.items():
        cls = _REGISTRY.get(node_type)
        if cls is None:
            continue
        cls.spec = builder()
        refreshed.append(node_type)
    return sorted(refreshed)


def all_params(spec: NodeTypeSpec) -> Iterable[ParamSpec]:
    return iter(spec.params)


#: Parameters every node accepts, appended by :func:`all_params_for` rather than
#: declared 14 times.
#:
#: They live here instead of in each spec because the alternative is fourteen
#: copies of the same four parameters, and the failure mode of that is a node
#: that silently does not offer `on_error` — which looks like the feature not
#: working rather than like a missing declaration.
_RESILIENCE_PARAMS: tuple[ParamSpec, ...] = (
    JsonParam(
        name="node_timeout_seconds",
        label="Time limit",
        label_zh="超时秒数",
        description=(
            "How long one attempt may take. 0 uses the deployment default "
            f"({_DEFAULT_TIMEOUT:g}s)."
        ),
        description_zh=(
            "单次尝试允许执行多久。0 表示用部署默认值"
            f"（{_DEFAULT_TIMEOUT:g} 秒）。"
        ),
        default=0,
        advanced=True,
    ),
    JsonParam(
        name="node_max_retries",
        label="Retries",
        label_zh="重试次数",
        description=(
            "Extra attempts after a failure. Off by default, because retrying "
            "repeats a side effect: a node that charged a card will charge it "
            "again."
        ),
        description_zh=(
            "失败后额外尝试的次数。默认关闭，因为重试会重复副作用："
            "已经扣过款的节点会再扣一次。"
        ),
        default=0,
        advanced=True,
    ),
    SelectParam(
        name="on_error",
        label="If it fails",
        label_zh="失败时",
        description=(
            "What happens when the attempts are spent. `stop` fails the run "
            "and names the node; the other two let the rest of the graph "
            "continue."
        ),
        description_zh=(
            "尝试次数用完后怎么办。`stop` 会让整个运行失败并指出是哪个节点；"
            "另外两个让图继续跑下去。"
        ),
        default=ON_ERROR_STOP,
        options=[
            SelectOption(
                value=ON_ERROR_STOP,
                label="Fail the run",
                label_zh="让运行失败",
                description="Default. A node that cannot run should not silently vanish.",
                description_zh="默认。跑不了的节点不应该悄悄消失。",
            ),
            SelectOption(
                value=ON_ERROR_CONTINUE,
                label="Skip and continue",
                label_zh="跳过并继续",
                description="The failure is recorded in the node's output.",
                description_zh="失败会被记录在该节点的输出里。",
            ),
            SelectOption(
                value=ON_ERROR_DEFAULT,
                label="Continue with a value",
                label_zh="带默认值继续",
                description="Like skip, but downstream nodes get `on_error_value`.",
                description_zh="等同于跳过，但下游节点会拿到 `on_error_value`。",
            ),
        ],
        advanced=True,
    ),
    JsonParam(
        name="on_error_value",
        label="Value on failure",
        label_zh="失败时的值",
        description="Only used by 'Continue with a value'.",
        description_zh="仅「带默认值继续」时使用。",
        default=None,
        advanced=True,
    ),
)


def all_params_for(spec: NodeTypeSpec) -> list[ParamSpec]:
    """A node type's parameters, plus the ones every node accepts.

    The catalogue calls this instead of reading ``spec.params`` directly, so a
    new resilience knob reaches every node by being added in one place. A type
    that declares one of these itself keeps its own copy and position — the
    declaration wins, which is what lets Code keep `fail_on_error` beside the
    others it has always had.
    """
    declared = {param.name for param in spec.params}
    extra = [param for param in _RESILIENCE_PARAMS if param.name not in declared]
    return [*spec.params, *extra]


def all_params(spec: NodeTypeSpec) -> Iterable[ParamSpec]:
    """Every parameter of a node type. Kept for existing callers."""
    return iter(all_params_for(spec))

# ---------------------------------------------------------------------------
# Effective ports (dynamic outputs)
# ---------------------------------------------------------------------------


def effective_outputs(node: NodeSpec) -> list[OutputPort]:
    """Output handles a *node instance* exposes.

    Most nodes use their static ``spec.outputs``. A Condition node instead
    derives one handle per configured branch, which is why the canvas asks the
    backend for ports rather than assuming the type defaults.
    """
    spec = get_node_type_spec(node.type)
    if spec.dynamic_outputs is DynamicOutputs.NONE or not spec.dynamic_outputs_param:
        return spec.outputs

    raw = (node.config or {}).get(spec.dynamic_outputs_param) or []
    ports: list[OutputPort] = []
    for index, entry in enumerate(_as_dict_list(raw)):
        handle_id = entry.get("id") or entry.get("name") or f"branch_{index}"
        label = entry.get("label") or entry.get("name") or f"Branch {index + 1}"
        ports.append(OutputPort(id=str(handle_id), label=str(label)))
    if not ports:
        # Unconfigured node still needs a stable handle so the UI can render.
        ports = list(spec.outputs)
    return ports


def effective_inputs(node: NodeSpec) -> list[OutputPort]:
    """Input handles for a node instance (currently always static)."""
    return get_node_type_spec(node.type).inputs


def _as_dict_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({"name": item, "label": item})
    return out


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


def create_node_instance(node: NodeSpec) -> BaseNode:
    return get_node_class(node.type)(node)


def registry_snapshot() -> dict[str, Any]:
    return {
        "count": len(_REGISTRY),
        "types": _ORDER,
    }
