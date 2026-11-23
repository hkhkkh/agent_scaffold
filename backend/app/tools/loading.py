"""Loading an agent's own tools from a package directory.

The problem
-----------
`register_tool()` writes into one process-wide registry. For the canvas that is
right — a tool is a capability the deployment offers, and every workflow may use
it. For an **exported agent** it is wrong in a specific way: the tools a business
agent needs are "query our order table", "submit a refund", and those belong to
*that agent's package*, not to whatever host it happens to run on.

So an agent declares its own registry, built by importing its own `tools/`
package. Two consequences that are the whole point:

* **Two agents cannot see each other's tools**, because neither reads the other's
  directory.
* **Installing a plugin on the host does not widen a running agent**, because the
  agent never consults the host's registry in the first place.

Loading is loud
---------------
A tool module that fails to import **stops the agent** rather than being skipped.
The alternative — a warning and a shorter tool list — produces an agent that
looks fine, starts fine, and silently cannot do something it was built to do. The
failure would surface as "the model would not call the tool" and be debugged in
entirely the wrong place.

The one thing that must not be loud is a *malformed schema*: a business tool whose
JSON schema is wrong is reported, and the rest of the agent still runs, because a
broken tool should cost you that tool and not the whole deployment. The
distinction is between "this tool is wrong" (report it) and "this tool is missing"
(fail), which are different problems with different fixes.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Any

from app.tools.registry import (
    BuiltinToolRegistry,
    ToolDef,
    ToolError,
    ToolRegistry,
)

#: Name of the sub-package an agent's tools live in.
TOOLS_PACKAGE = "tools"


class ToolLoadError(RuntimeError):
    """Raised when an agent's tool package cannot be imported at all.

    Distinct from a single bad tool: this one means the agent should not start,
    because its tool set is not what it was built with.
    """


def _schema_problems(tool: ToolDef) -> list[str]:
    """Reasons this tool's declared schema is not usable.

    Checked here rather than left to the first call, because the failure mode of
    a bad schema is that the *model* mis-calls the tool — an error that looks like
    a prompt problem and is not.
    """
    problems: list[str] = []
    schema = tool.parameters
    if not isinstance(schema, dict):
        return [f"'{tool.name}': parameters is not an object"]
    if schema.get("type") != "object":
        problems.append(f"'{tool.name}': parameters.type must be 'object'")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        problems.append(f"'{tool.name}': parameters.properties must be an object")
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            problems.append(f"'{tool.name}': parameters.required must be a list")
        elif isinstance(properties, dict):
            unknown = [name for name in required if name not in properties]
            if unknown:
                problems.append(
                    f"'{tool.name}': required names {unknown} which are not properties"
                )
    if not tool.name:
        problems.append("a tool has an empty name")
    return problems


def load_tools_from(
    directory: Path | str,
    *,
    base: ToolRegistry | None = None,
    strict: bool = True,
) -> tuple[BuiltinToolRegistry, list[str]]:
    """Build a registry from the tool modules in ``directory``.

    ``base`` is the registry to build on — normally *empty*, because an agent's
    registry is its own. Passing one lets a deployment offer shared tools
    alongside; it is a parameter rather than a baked-in default so the isolation
    test can pin "nothing leaks in unless it was asked for".

    Returns ``(registry, problems)``. ``problems`` holds per-tool complaints that
    did not stop loading (see the module docstring for why those are separated
    from a hard failure). Raises :class:`ToolLoadError` when the directory cannot
    be imported at all.
    """
    directory = Path(directory)
    # A *fresh* store, not the process-wide one. `BuiltinToolRegistry()` defaults
    # to the global `_REGISTRY`, which is right for the product (one tool set per
    # process) and exactly wrong here: an agent's tools would land in the shared
    # registry, and the next agent loaded would collide with them. The first
    # version did that and the isolation test caught it as
    # `Duplicate tool name: 'query_orders'` on the second load.
    if base is None:
        registry = BuiltinToolRegistry(store={})
    elif isinstance(base, BuiltinToolRegistry):
        registry = base
    else:
        # Any other registry is a view; give the agent its own store underneath it
        # so loading cannot write through the view into someone else's store.
        registry = BuiltinToolRegistry(store={})
    problems: list[str] = []

    if not directory.is_dir():
        if strict:
            raise ToolLoadError(f"Tool directory '{directory}' does not exist.")
        return registry, [f"tool directory '{directory}' is missing"]

    # Imported under the literal name `tools`, not a generated one, because a
    # tool module may legitimately do `from tools import ...` or hold a relative
    # import to a sibling. A synthetic name would break both, and it is the
    # package name the exporter writes (`tools/__init__.py`).
    package_dir = directory
    if str(directory.parent) not in sys.path:
        sys.path.insert(0, str(directory.parent))

    # Two agents in one process would otherwise share whichever `tools` was
    # imported first. Dropping the cache entry is what makes "each agent gets its
    # own registry" true rather than incidentally true.
    for name in [
        key for key in list(sys.modules) if key == TOOLS_PACKAGE or key.startswith(f"{TOOLS_PACKAGE}.")
    ]:
        del sys.modules[name]

    spec = importlib.util.spec_from_file_location(
        TOOLS_PACKAGE,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ToolLoadError(
            f"'{package_dir}' has no importable __init__.py, so its tools cannot be loaded."
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[TOOLS_PACKAGE] = module

    # The module registers into a registry we control, by temporarily installing
    # it as the process registry for the duration of the import. `register_tool`
    # delegates to `get_tool_registry()`, so this is the seam that makes plain
    # `register_tool(...)` calls at module level land in the agent's registry
    # rather than the host's.
    from app.tools import registry as registry_module

    previous = registry_module.get_tool_registry()
    registry_module.set_tool_registry(registry)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        raise ToolLoadError(
            f"An agent tool module failed to import: {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc(limit=3)}"
        ) from exc
    finally:
        registry_module.set_tool_registry(previous)

    # Validate what was registered, and drop the unusable ones rather than
    # carrying them into the model's tool list.
    for tool in list(registry.list()):
        issues = _schema_problems(tool)
        if issues:
            problems.extend(issues)
            registry.unregister(tool.name)

    return registry, problems


async def check_tool_calls(tool: ToolDef, sample: dict[str, Any] | None = None) -> list[str]:
    """Call a tool with no arguments and report what it says about itself.

    Not used by the loader: a business tool usually needs a live endpoint, so
    calling every one at startup would make an agent's availability depend on a
    downstream system. Offered as a debugging aid for the tool editor.
    """
    if tool.fn is None:
        return [f"'{tool.name}' has no implementation"]
    try:
        await tool.fn(**(sample or {}))
        return []
    except ToolError as exc:
        return [str(exc)]
    except Exception as exc:  # noqa: BLE001
        return [f"{type(exc).__name__}: {exc}"]


__all__ = [
    "TOOLS_PACKAGE",
    "ToolLoadError",
    "check_tool_calls",
    "load_tools_from",
]
