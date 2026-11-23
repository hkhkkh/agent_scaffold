"""Tool registry: capabilities the Tool and Agent nodes can call."""

from app.tools.builtin import BUILTIN_TOOLS, ToolDef
# Imported for its side effect: the module registers `db_write`, `db_query`,
# `db_tables` and `db_reset` at import time, exactly like `app.tools.builtin`
# does for the network tools. A separate module rather than four more entries in
# `builtin.py` because these four share a file, a schema inference rule and a
# read-only snapshot function that the API also calls — see its docstring for
# why a database tool exists at all when the Code node cannot open one.
from app.tools import results_db as _results_db  # noqa: F401
from app.tools.registry import (
    BuiltinToolRegistry,
    ToolError,
    get_tool,
    list_tools,
    register_tool,
    tool_schemas,
)


def register_capability_pack_tools() -> list[str]:
    """Put the built-in packs' tools into the process registry.

    Deliberately **not** called at import time. `app.tools` is imported by
    `app.runtime.packs.builtin.core` (it resolves the core tools from this
    registry), so a call here would recurse while the pack module is still
    initialising. The platform calls this from `app/main.py`'s lifespan; an
    exported agent builds its own scoped registry from the packs its
    `manifest.yml` names, so it does not need the process-wide palette at all.

    Returns the names it added, which is what `doctor` and tests report.
    """
    from app.runtime.packs.registry import register_builtin_tools

    return register_builtin_tools(BuiltinToolRegistry())


__all__ = [
    "BUILTIN_TOOLS",
    "BuiltinToolRegistry",
    "ToolDef",
    "ToolError",
    "get_tool",
    "list_tools",
    "register_capability_pack_tools",
    "register_tool",
    "tool_schemas",
]
