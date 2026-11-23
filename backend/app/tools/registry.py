"""Tool registry — the ability layer's tool capability.

A tool is a plain async callable plus an OpenAI-style JSON schema. The schema
does double duty:

* it is handed to the LLM in the Agent node (via ``bind_tools``), and
* it drives argument validation and the Inspector's help text in the UI.

Registering a tool is a one-liner, so extending the scaffold's capabilities does
not require touching the Agent or Tool nodes.

Why there is an interface here
------------------------------

Because "every tool that happens to be registered on this host" is a bad answer
to the question *what may this agent call?*

Three things force the question:

1. **An exported agent is its own deployment.** Phase 6 ships an agent as a
   package that runs somewhere else. Its tool set has to travel with it as data,
   not be re-derived from whatever plugins the destination happens to have
   installed.
2. **Installing a plugin must not silently widen a running agent.** With one
   global registry, adding a module with a ``register_tool`` call grants a new
   capability to every agent on the machine.
3. **Phase 5 wants an audit trail.** "Which agent called which tool" needs a
   place to sit that is not inside the node.

So the storage stays here (it is what :mod:`app.tools.builtin` fills at import),
and the *answer* becomes a swappable object: :class:`ToolRegistry`.

The free functions at the bottom of this module (``list_tools``, ``get_tool``,
``tool_schemas``, ``register_tool``) now **delegate** to
:func:`get_tool_registry`. That is deliberate: a caller that keeps using them
automatically respects whatever registry the deployment installed. There is no
second path that silently bypasses the interface.

``coerce_arguments`` and ``validate_arguments`` are **not** part of the
interface. They are pure functions over a :class:`ToolDef` — no I/O, no
resource, no external state — so abstracting them would add a layer of
indirection and nothing else.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.settings import Settings, get_settings

__all__ = [
    "BuiltinToolRegistry",
    "ScopedToolRegistry",
    "ToolDef",
    "ToolError",
    "ToolRegistry",
    "build_tool_registry",
    "coerce_arguments",
    "get_tool",
    "get_tool_registry",
    "list_tools",
    "register_tool",
    "set_tool_registry",
    "tool_schemas",
    "unknown_tool_names",
    "validate_arguments",
]


class ToolError(RuntimeError):
    """Raised when a tool is missing or its arguments are invalid."""


@dataclass
class ToolDef:
    """One callable capability."""

    name: str
    label: str
    description: str
    #: Chinese overrides for the UI only. Deliberately **not** used by
    #: `openai_schema`: the model sees `description` in English regardless of the
    #: interface language. Localising what the model reads would mean switching
    #: the UI to Chinese silently changes which tools an agent picks, which is
    #: not a change a language setting should ever cause.
    label_zh: str = ""
    description_zh: str = ""
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    fn: Callable[..., Awaitable[Any]] | None = None
    #: Tools that reach the network or the filesystem are flagged so the UI can
    #: warn before a workflow is run.
    dangerous: bool = False

    async def __call__(self, **kwargs: Any) -> Any:
        if self.fn is None:
            raise ToolError(f"Tool '{self.name}' has no implementation.")
        result = self.fn(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def openai_schema(self) -> dict[str, Any]:
        """The ``{"type": "function", "function": {...}}`` form LLMs expect."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def summary(self) -> dict[str, Any]:
        """Serialisable view for the frontend palette / Inspector.

        Carries both languages; the catalogue endpoint resolves them on the way
        out, the same way it does for node types.
        """
        return {
            "name": self.name,
            "label": self.label,
            "label_zh": self.label_zh,
            "description": self.description,
            "description_zh": self.description_zh,
            "parameters": self.parameters,
            "dangerous": self.dangerous,
        }


_REGISTRY: dict[str, ToolDef] = {}


class ToolRegistry:
    """The tools an agent may call.

    Implementations must:

    * raise :class:`ToolError` — never return ``None`` — for a tool that is
      unknown or not available, so a caller cannot mistake absence for a result;
    * return tools in a stable order, because ``list()`` feeds a dropdown and an
      agent's prompt, and a list that reshuffles between runs makes both harder
      to reason about.
    """

    def get(self, name: str) -> ToolDef:
        raise NotImplementedError

    def list(self) -> list[ToolDef]:
        raise NotImplementedError

    def register(self, tool: ToolDef) -> ToolDef:
        raise NotImplementedError

    # ------------------------------------------------------------- conveniences
    def names(self) -> list[str]:
        return [tool.name for tool in self.list()]

    def schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """OpenAI function schemas for the named tools (all, when ``None``)."""
        selected = self.list() if names is None else [self.get(n) for n in names]
        return [tool.openai_schema() for tool in selected]

    def __contains__(self, name: object) -> bool:
        try:
            self.get(str(name))
        except ToolError:
            return False
        return True


@dataclass
class BuiltinToolRegistry(ToolRegistry):
    """The process-wide store that :func:`register_tool` writes to at import.

    ``register`` and the module-level free functions write to the same dict on
    purpose: :mod:`app.tools.builtin` populates this at import time, and the
    registry a deployment selects is built on top of that store rather than
    replacing it.

    ``store`` is injectable so a caller that wants an isolated registry can have
    one. Without it, every instance shares module state — which is correct for
    the product (there is one tool set per process) and wrong for a test, where
    registering a temporary tool would leak into every test that runs after it.
    """

    store: dict[str, ToolDef] = field(default_factory=lambda: _REGISTRY)

    def get(self, name: str) -> ToolDef:
        try:
            return self.store[name]
        except KeyError as exc:
            raise ToolError(
                f"Unknown tool '{name}'. "
                f"Available: {', '.join(sorted(self.store)) or 'none'}"
            ) from exc

    def list(self) -> list[ToolDef]:
        return sorted(self.store.values(), key=lambda tool: tool.label)

    def register(self, tool: ToolDef) -> ToolDef:
        if tool.name in self.store:
            raise ValueError(f"Duplicate tool name: '{tool.name}'")
        self.store[tool.name] = tool
        return tool

    def unregister(self, name: str) -> bool:
        """Remove a tool. Returns whether it was there.

        Exists for the loader in `app/tools/loading.py`, which drops a tool whose
        declared schema is unusable rather than leaving it in the model's tool
        list. Not part of the `ToolRegistry` interface: a *view* has no business
        deleting from the registry it views, and only the store can.
        """
        return self.store.pop(name, None) is not None


@dataclass
class ScopedToolRegistry(ToolRegistry):
    """A bounded **view** of another registry.

    This is the shape an exported agent needs: its manifest names the tools it
    may call, and a deployment builds one of these over whatever it has locally.
    The agent then cannot reach a tool that arrived with a plugin after it was
    packaged — the allowlist is data, not an accident of the host.

    ``allow`` empty means "everything the inner registry has", which keeps the
    zero-configuration case (a general-purpose agent) working. ``deny`` is
    subtracted afterwards, so it can switch off one dangerous tool —
    ``http_request`` on a machine with no network, say — without enumerating the
    rest.

    ``register`` forwards to the inner registry rather than raising. A view does
    not own its contents; whether a newly registered tool is *visible* is then
    the allowlist's decision, which is the correct division. Raising here would
    instead turn a plugin that registers late into a startup failure.
    """

    inner: ToolRegistry
    allow: frozenset[str] = field(default_factory=frozenset)
    deny: frozenset[str] = field(default_factory=frozenset)

    def permits(self, name: str) -> bool:
        if name in self.deny:
            return False
        return not self.allow or name in self.allow

    def list(self) -> list[ToolDef]:
        return [tool for tool in self.inner.list() if self.permits(tool.name)]

    def get(self, name: str) -> ToolDef:
        # Resolved against the inner registry *first*, so that "there is no such
        # tool" and "that tool exists but this agent may not use it" produce
        # different messages. Conflating them sends an operator looking for a
        # typo when the answer is a policy.
        tool = self.inner.get(name)
        if not self.permits(name):
            enabled = ", ".join(self.names()) or "none"
            raise ToolError(
                f"Tool '{name}' is registered but not enabled for this agent. "
                f"Enabled: {enabled}."
            )
        return tool

    def register(self, tool: ToolDef) -> ToolDef:
        return self.inner.register(tool)


def unknown_tool_names(
    names: list[str], *, base: ToolRegistry | None = None
) -> list[str]:
    """Names that no tool answers to — typos, in practice.

    Checked against the **base** registry, never the configured one. Asking the
    scoped registry whether ``"calclator"`` is known returns yes, because that is
    precisely what the allowlist said — so the check would pass for the one input
    it exists to catch. (Found by asserting the negative case; the first version
    of this function was decorative.)

    Called at startup rather than from the constructor, because the registry is
    built while :mod:`app.tools.builtin` is still importing: validating earlier
    would report every built-in tool as unknown.
    """
    source = base or BuiltinToolRegistry()
    return sorted(name for name in names if name not in source)


def build_tool_registry(settings: Settings | None = None) -> ToolRegistry:
    """Construct the registry from configuration.

    Returns the plain built-in registry when no allow/deny list is configured,
    so the default deployment has no filtering layer at all rather than an
    inert one.
    """
    settings = settings or get_settings()
    allow = frozenset(
        name.strip() for name in settings.tools_allowlist if name.strip()
    )
    deny = {name.strip() for name in settings.tools_denylist if name.strip()}
    if settings.public_demo:
        # Belt and braces with TOOLS_DENYLIST: destructive data changes must not
        # be one env typo away from a public visitor. Reads remain available.
        for destructive in ("db_reset", "db_update", "db_delete"):
            deny.add(destructive)
    deny_set = frozenset(deny)

    base = BuiltinToolRegistry()
    if not allow and not deny_set:
        return base
    return ScopedToolRegistry(inner=base, allow=allow, deny=deny_set)


#: Process-wide registry. Swapped in tests, and the seam where an exported
#: agent's manifest, a remote registry, or an audit-wrapped registry plugs in.
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_tool_registry()
    return _registry


def set_tool_registry(registry: ToolRegistry | None) -> None:
    """Override the process registry. Passing ``None`` restores the configured one."""
    global _registry
    _registry = registry


# ---------------------------------------------------------------------------
# Free functions — thin delegations, so no caller can bypass the interface
# ---------------------------------------------------------------------------


def register_tool(tool: ToolDef) -> ToolDef:
    """Add a tool to whatever registry is active."""
    return get_tool_registry().register(tool)


def get_tool(name: str) -> ToolDef:
    return get_tool_registry().get(name)


def list_tools() -> list[ToolDef]:
    return get_tool_registry().list()


def tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """OpenAI function schemas for the named tools (all, when ``None``)."""
    return get_tool_registry().schemas(names)


def coerce_arguments(raw: Any) -> dict[str, Any]:
    """Accept a dict or a JSON string — models emit both."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Arguments are not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ToolError(f"Expected a JSON object, got {type(parsed).__name__}.")
        return parsed
    raise ToolError(f"Unsupported argument type: {type(raw).__name__}")


def validate_arguments(tool: ToolDef, arguments: dict[str, Any]) -> None:
    """Check required properties are present before calling the tool."""
    schema = tool.parameters or {}
    required = schema.get("required") or []
    missing = [key for key in required if key not in arguments or arguments[key] is None]
    if missing:
        raise ToolError(
            f"Tool '{tool.name}' is missing required argument(s): {', '.join(missing)}"
        )
