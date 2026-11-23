"""The capability pack registry.

What it owns
------------
* which packs exist (the built-in ones, plus anything a deployment registers);
* which pack provides a given tool, so a graph can be read as "needs files";
* building the tool registry a given deployment should actually have -- the
  platform's full palette, or an exported agent's narrowed set;
* the doctor report: dependencies installed? permissions known? tools present?

Process-wide injection point
----------------------------
Same shape as the other capability seams (`app.tools.registry`,
`app.runtime.egress`): a process-level instance, `get_*` / `set_*`, and a
`build_*` that reads configuration. Tests swap it; an exported agent builds one
from its own `manifest.yml`; the platform uses the built-in set.

Lazy tool materialisation
-------------------------
A pack's `tools_factory` is called when tools are first requested, not when the
pack is registered. That matters for the files pack: its module can be imported
without `openpyxl` installed, so the platform starts on a laptop and the doctor
reports the missing dependency instead of a broken import.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

from app.runtime.packs.manifest import ManifestError, PackDependency, PackManifest
from app.runtime.permissions import ALL_PERMISSIONS

if TYPE_CHECKING:  # pragma: no cover - annotations only
    # Imported for annotations and **never at module import time**. `app.tools`
    # imports this module (at the bottom of its `__init__`) to register pack
    # tools into the process registry; a module-level `app.tools` import here
    # would be a cycle while that is happening. The registry also only needs a
    # duck-typed object for `register_tools` (get/register/names), so the type
    # is the only thing worth importing.
    from app.tools.registry import ToolDef, ToolRegistry

__all__ = [
    "CapabilityPack",
    "PackError",
    "PackRegistry",
    "builtin_registry",
    "doctor_report",
    "format_doctor",
    "get_pack_registry",
    "register_builtin_tools",
    "set_pack_registry",
    "BUILTIN_PACK_NAMES",
]


class PackError(RuntimeError):
    """A pack is missing, inconsistent, or asks for something unknown."""


@dataclass
class CapabilityPack:
    """A manifest plus the code that provides its tools."""

    manifest: PackManifest
    tools_factory: Callable[[], Sequence[ToolDef]] | None = None
    _tools: list[ToolDef] | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.manifest.name

    def tools(self) -> list[ToolDef]:
        """The pack's tools, built once and reused.

        Reuse matters: register the same pack into a second registry (the canvas
        registry and an exported agent's registry in one process) and the tool
        objects are shared, so a mutable default cannot drift between them.
        """
        if self._tools is None:
            provided = list(self.tools_factory() if self.tools_factory else [])
            self._tools = provided
        return list(self._tools)

    def problems(self) -> list[str]:
        """Inconsistencies between the manifest and the code it ships with."""
        problems: list[str] = []
        declared = set(self.manifest.tools)
        actual = {tool.name for tool in self.tools()}
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        if missing:
            problems.append(f"manifest declares tools this pack does not provide: {missing}")
        if extra:
            problems.append(f"pack provides tools its manifest does not declare: {extra}")
        unknown = sorted(set(self.manifest.permissions) - ALL_PERMISSIONS)
        if unknown:
            problems.append(f"manifest asks for permissions this runtime does not know: {unknown}")
        return problems


class PackRegistry:
    """A set of capability packs, addressed by name."""

    def __init__(self, packs: Iterable[CapabilityPack] = ()) -> None:
        self._packs: dict[str, CapabilityPack] = {}
        self._tool_owner: dict[str, str] = {}
        for pack in packs:
            self.register(pack)

    # ------------------------------------------------------------ registration
    def register(self, pack: CapabilityPack) -> CapabilityPack:
        name = pack.manifest.name
        if name in self._packs:
            raise PackError(f"duplicate pack name '{name}'")
        for tool in pack.tools():
            if tool.name in self._tool_owner:
                raise PackError(
                    f"tool '{tool.name}' is provided by both "
                    f"'{self._tool_owner[tool.name]}' and '{name}'"
                )
        self._packs[name] = pack
        for tool in pack.tools():
            self._tool_owner[tool.name] = name
        return pack

    # ------------------------------------------------------------- inspection
    def get(self, name: str) -> CapabilityPack:
        try:
            return self._packs[name]
        except KeyError as exc:
            raise PackError(
                f"unknown capability pack '{name}'. Known: {', '.join(self.names()) or 'none'}"
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._packs

    def names(self) -> list[str]:
        return sorted(self._packs)

    def list(self) -> list[CapabilityPack]:
        return [self._packs[name] for name in self.names()]

    def manifests(self) -> list[dict[str, Any]]:
        return [pack.manifest.to_dict() for pack in self.list()]

    def tool_names(self) -> list[str]:
        return sorted(self._tool_owner)

    def pack_for_tool(self, tool_name: str) -> str | None:
        return self._tool_owner.get(tool_name)

    def packs_for_tools(self, names: Iterable[str]) -> list[str]:
        """Which packs a set of tool names needs, sorted and deduplicated."""
        found: set[str] = set()
        for name in names:
            owner = self._tool_owner.get(name)
            if owner:
                found.add(owner)
        return sorted(found)

    def resolve_tool(self, tool_name: str) -> ToolDef:
        """The ToolDef for a name any pack provides, regardless of registry."""
        for pack in self.list():
            for tool in pack.tools():
                if tool.name == tool_name:
                    return tool
        raise PackError(f"no capability pack provides tool '{tool_name}'")

    # ------------------------------------------------------------- tool wiring
    def register_tools(
        self,
        target: ToolRegistry,
        *,
        packs: Iterable[str] | None = None,
        tool_names: Iterable[str] | None = None,
        strict: bool = True,
    ) -> list[str]:
        """Register pack tools into ``target``.

        ``packs`` limits which packs are consulted; ``tool_names`` limits which
        tools are *copied over* (an exported agent should carry only what its
        definition names). ``strict`` turns "you asked for a tool no pack has"
        into an error instead of a note -- the exporter uses it to fail early.
        """
        selected = [self.get(name) for name in packs] if packs is not None else self.list()
        wanted = set(tool_names) if tool_names is not None else None
        registered: list[str] = []
        provided: set[str] = set()
        for pack in selected:
            for tool in pack.tools():
                provided.add(tool.name)
                if wanted is not None and tool.name not in wanted:
                    continue
                if tool.name in target:
                    continue
                target.register(tool)
                registered.append(tool.name)
        if strict and wanted is not None:
            missing = sorted(wanted - provided - set(target.names()))
            if missing:
                raise PackError(
                    "requested tools are not provided by the selected packs "
                    f"({', '.join(pack.name for pack in selected) or 'none'}): {missing}"
                )
        return registered

    # ------------------------------------------------------------ dependencies
    def dependencies(self, packs: Iterable[str] | None = None) -> list[PackDependency]:
        selected = [self.get(name) for name in packs] if packs is not None else self.list()
        seen: dict[str, PackDependency] = {}
        for pack in selected:
            for dependency in pack.manifest.dependencies:
                key = dependency.spec
                if key not in seen:
                    seen[key] = dependency
                elif not dependency.optional:
                    seen[key] = PackDependency(
                        spec=seen[key].spec,
                        import_name=seen[key].import_name,
                        optional=False,
                    )
        return list(seen.values())

    def missing_dependencies(self, packs: Iterable[str] | None = None) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for dependency in self.dependencies(packs):
            available = _importable(dependency.import_name)
            if not available and not dependency.optional:
                missing.append(dependency.to_dict())
        return missing

    # ------------------------------------------------------------------ doctor
    def doctor(self) -> dict[str, Any]:
        return doctor_report(self)


def _importable(module_name: str) -> bool:
    if not module_name:
        return False
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):  # a parent package that does not exist
        return False


#: Names of the packs this runtime ships with. Kept as a literal because it is
#: also what `manifest.yml` and the docs refer to; a test asserts that every
#: module under `packs/builtin/` is listed here.
BUILTIN_PACK_NAMES: tuple[str, ...] = ("core", "files")


def builtin_registry() -> PackRegistry:
    """The registry of packs shipped with this runtime.

    Imported lazily: `packs.builtin` imports `packs.registry` for the
    CapabilityPack class, so a module-level import here would be circular.
    """
    from app.runtime.packs.builtin import BUILTIN_PACKS

    return PackRegistry(BUILTIN_PACKS)


#: Process-wide registry.
_registry: PackRegistry | None = None


def get_pack_registry() -> PackRegistry:
    global _registry
    if _registry is None:
        _registry = builtin_registry()
    return _registry


def set_pack_registry(registry: PackRegistry | None) -> None:
    """Override the process registry. ``None`` restores the built-in set."""
    global _registry
    _registry = registry


def register_builtin_tools(target: ToolRegistry) -> list[str]:
    """Register every built-in pack's tools into ``target``.

    Called once by `app.tools` when the process starts, so the platform's tool
    palette includes packs without every consumer knowing packs exist.
    """
    return get_pack_registry().register_tools(target)


def doctor_report(registry: PackRegistry | None = None) -> dict[str, Any]:
    """A machine-readable health report for the packs in this deployment."""
    registry = registry or get_pack_registry()
    packs: list[dict[str, Any]] = []
    all_problems: list[str] = []
    for pack in registry.list():
        dependencies: list[dict[str, Any]] = []
        problems = pack.problems()
        for dependency in pack.manifest.dependencies:
            available = _importable(dependency.import_name)
            dependencies.append({**dependency.to_dict(), "available": available})
            if not available and not dependency.optional:
                problems.append(
                    f"missing dependency {dependency.spec!r} "
                    f"(python import '{dependency.import_name}')"
                )
        entry = {
            **pack.manifest.to_dict(),
            "provided_tools": sorted(tool.name for tool in pack.tools()),
            "dependencies_status": dependencies,
            "problems": problems,
        }
        packs.append(entry)
        all_problems.extend(f"{pack.name}: {problem}" for problem in problems)
    return {
        "ok": not all_problems,
        "packs": packs,
        "tool_count": len(registry.tool_names()),
        "problems": all_problems,
    }


def format_doctor(report: dict[str, Any]) -> str:
    """Render a doctor report for a terminal. One line per fact, no tables."""
    lines: list[str] = []
    for entry in report.get("packs", []):
        status = "OK  " if not entry.get("problems") else "FAIL"
        lines.append(
            f"{status} pack {entry['name']} {entry.get('version', '')} "
            f"({len(entry.get('provided_tools') or [])} tools)"
        )
        for dependency in entry.get("dependencies_status") or []:
            mark = "installed" if dependency["available"] else "MISSING"
            lines.append(f"     dependency {dependency['spec']} -> {mark}")
        for problem in entry.get("problems") or []:
            lines.append(f"     problem: {problem}")
    lines.append(f"tools registered: {report.get('tool_count', 0)}")
    lines.append("RESULT=" + ("PASS" if report.get("ok") else "FAIL"))
    return "\n".join(lines)
