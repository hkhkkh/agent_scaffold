"""The core pack: the tools the canvas has always had.

Keeping these in a pack is not cosmetic. It makes the *whole* tool set expressible
as packs, so the registry can answer "which capability does this graph need?"
for a graph that only uses `calculator` just as well as for one that reads
spreadsheets. It also gives `manifest.yml` a uniform shape from the first release
instead of one special case that every later reader has to know about.

The tools themselves are registered by `app.tools.builtin` and
`app.tools.results_db` at import, the same as before -- this pack points at them
rather than moving them, because moving 10 tool modules would have touched the
registry seam for no behavioural gain.
"""

from __future__ import annotations

from app.runtime.packs.manifest import PackManifest
from app.runtime.packs.registry import CapabilityPack
from app.runtime.permissions import DB_READ, DB_WRITE, NET_HTTP, WEB_SEARCH
from app.tools.registry import BuiltinToolRegistry, ToolError, ToolDef

__all__ = ["CORE_MANIFEST", "CORE_PACK", "CORE_TOOL_NAMES"]

#: The tools that were already present before packs existed. A test asserts this
#: list matches what `app.tools` registers, so adding a built-in tool and
#: forgetting the pack fails rather than leaving an undeclared capability.
CORE_TOOL_NAMES: tuple[str, ...] = (
    # network / calculation
    "calculator",
    "current_time",
    "http_request",
    "json_query",
    "text_stats",
    "web_search",
    # results database
    "db_query",
    "db_tables",
    "db_write",
    "db_reset",
    # Result-row mutations. They landed after the original four and were
    # exactly the kind of addition this list exists to catch: a tool in the
    # registry but in no pack is a tool the export cannot account for.
    "db_update",
    "db_delete",
)


def _core_tools() -> list[ToolDef]:
    registry = BuiltinToolRegistry()
    tools: list[ToolDef] = []
    for name in CORE_TOOL_NAMES:
        try:
            tools.append(registry.get(name))
        except ToolError as exc:
            # Loud: the pack and the registry disagree, which means the process
            # is not the one that was built. A silent skip would make an agent
            # start with a palette that quietly lacks a documented tool.
            raise RuntimeError(
                f"core pack declares tool '{name}' but the tool registry does not have it"
            ) from exc
    return tools


CORE_MANIFEST = PackManifest(
    name="core",
    version="1.0.0",
    title="Core tools",
    title_zh="核心工具",
    description=(
        "Network, calculation and results-database tools every deployment has. "
        "Always available; not something an agent opts into."
    ),
    description_zh="每个部署都有的网络、计算与结果数据库工具，始终可用，不需要 agent 声明。",
    tools=CORE_TOOL_NAMES,
    permissions=(NET_HTTP, WEB_SEARCH, DB_READ, DB_WRITE),
    notes="Previously the only tool set; unchanged in behaviour.",
)

CORE_PACK = CapabilityPack(CORE_MANIFEST, tools_factory=_core_tools)
