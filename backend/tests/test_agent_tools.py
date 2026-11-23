"""An exported agent's own tools: loaded from its package, isolated per agent.

The two claims worth testing are the two that make this a feature rather than a
convenience:

* **Two agents cannot see each other's tools.** Otherwise "the agent carries its
  business tools" is false in the only case that matters — two agents on one host.
* **A tool module that will not import stops the agent.** Otherwise the failure
  presents as "the model would not call that tool", which is debugged in the
  prompt instead of in the package.

Plus the distinction the loader makes deliberately: a *malformed schema* costs you
that tool and nothing else.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from app.tools.loading import ToolLoadError, load_tools_from
from app.tools.registry import ToolError

PYTHON = sys.executable


def _tool_module(name: str, *, bad_schema: bool = False) -> str:
    schema = (
        '{"type": "object", "properties": {"x": {"type": "string"}}, "required": ["missing"]}'
        if bad_schema
        else '{"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}'
    )
    return textwrap.dedent(
        f'''
        from __future__ import annotations

        from typing import Any

        from app.tools.registry import ToolDef, register_tool


        async def _{name}(x: str = "") -> dict[str, Any]:
            return {{"tool": "{name}", "x": x}}


        register_tool(
            ToolDef(
                name="{name}",
                label="{name}",
                description="{name}",
                parameters={schema},
                fn=_{name},
            )
        )
        '''
    )


def _make_agent_tools(root, package: str, modules: dict[str, str]) -> None:
    directory = root / package / "tools"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(
        "\n".join(f"from . import {module}  # noqa: F401" for module in modules),
        encoding="utf-8",
    )
    for module, body in modules.items():
        (directory / f"{module}.py").write_text(body, encoding="utf-8")


class TestLoadingFromAPackage:
    def test_a_directory_registers_its_tools(self, tmp_path):
        _make_agent_tools(tmp_path, "a", {"lookup": _tool_module("lookup_order")})
        registry, problems = load_tools_from(tmp_path / "a" / "tools")

        assert problems == []
        assert [tool.name for tool in registry.list()] == ["lookup_order"]

    def test_two_agents_cannot_see_each_others_tools(self, tmp_path):
        """The claim that makes this a feature. Two agents, one process.

        A shared cache or a global registry each break this in a way that only
        shows up when two agents exist — which, on a host serving more than one
        business function, is the normal case.
        """
        _make_agent_tools(tmp_path, "a", {"orders": _tool_module("query_orders")})
        _make_agent_tools(tmp_path, "b", {"refunds": _tool_module("submit_refund")})

        registry_a, _ = load_tools_from(tmp_path / "a" / "tools")
        registry_b, _ = load_tools_from(tmp_path / "b" / "tools")

        assert [tool.name for tool in registry_a.list()] == ["query_orders"]
        assert [tool.name for tool in registry_b.list()] == ["submit_refund"]

        with pytest.raises(ToolError, match="submit_refund|Unknown tool"):
            registry_a.get("submit_refund")
        with pytest.raises(ToolError):
            registry_b.get("query_orders")

    def test_loading_again_does_not_accumulate(self, tmp_path):
        """Reloading the same agent must not double its tools.

        The import cache is cleared before each load so the second agent sees its
        own package; if that cache were shared, agent B would silently get A's
        modules — the isolation test above would still pass on the *first* load
        and fail on the second.
        """
        _make_agent_tools(tmp_path, "a", {"orders": _tool_module("query_orders")})
        first, _ = load_tools_from(tmp_path / "a" / "tools")
        second, _ = load_tools_from(tmp_path / "a" / "tools")
        assert [t.name for t in first.list()] == [t.name for t in second.list()]

    def test_a_deployment_can_layer_shared_tools_underneath(self, tmp_path):
        """`base` is a parameter, not a default, so isolation is the default."""
        from app.tools.registry import BuiltinToolRegistry, ToolDef

        base = BuiltinToolRegistry(store={})
        base.register(ToolDef(name="shared", label="Shared", description=""))
        _make_agent_tools(tmp_path, "a", {"orders": _tool_module("query_orders")})

        registry, _ = load_tools_from(tmp_path / "a" / "tools", base=base)
        assert {tool.name for tool in registry.list()} == {"query_orders", "shared"}


class TestFailuresAreLoudWhereItMatters:
    def test_a_module_that_will_not_import_stops_the_agent(self, tmp_path):
        """Not a warning. A shorter tool list is indistinguishable, at run time,
        from a model that refuses to call a tool."""
        _make_agent_tools(tmp_path, "a", {"broken": "this is not python"})
        with pytest.raises(ToolLoadError, match="broken|failed to import"):
            load_tools_from(tmp_path / "a" / "tools")

    def test_a_missing_directory_is_reported(self, tmp_path):
        with pytest.raises(ToolLoadError, match="does not exist"):
            load_tools_from(tmp_path / "nope")

    def test_a_malformed_schema_costs_one_tool_not_the_agent(self, tmp_path):
        """The deliberate asymmetry: a *wrong* tool is repaired, a *missing* one
        is fatal. Different problems, different fixes."""
        _make_agent_tools(
            tmp_path,
            "a",
            {
                "good": _tool_module("fine"),
                "bad": _tool_module("broken_schema", bad_schema=True),
            },
        )
        registry, problems = load_tools_from(tmp_path / "a" / "tools")

        assert [tool.name for tool in registry.list()] == ["fine"]
        assert any("broken_schema" in problem for problem in problems)


class TestAnExportedAgentUsesItsOwnTools:
    def test_the_package_ships_a_loadable_tools_directory(self, tmp_path):
        """The exported package's `tools/` must be importable as shipped.

        Written and checked as a subprocess because the whole point is that the
        package works on a machine that has nothing but Python and the vendored
        runtime — an in-process check would use this repository's imports.
        """
        from app.runtime.export import build_export, write_export
        from tests.test_export_agent import APP_ROOT, _graph

        root = tmp_path / "agent"
        write_export(build_export(_graph(), APP_ROOT), root)

        assert (root / "tools" / "__init__.py").exists()
        assert (root / "tools" / "lookup_order.py.example").exists()

        # Compile every generated Python file: a text template can be valid as a
        # string and not as a module, which is how the first version of the tools
        # template produced a package that would not import.
        for path in sorted(root.rglob("*.py")) + sorted(root.rglob("*.py.example")):
            result = subprocess.run(
                [PYTHON, "-c", "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())", str(path)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"{path.name} does not parse:\n{result.stderr}"

    def test_the_example_tool_is_a_real_module(self, tmp_path):
        """Copying it must produce a working tool, not a sketch.

        Checked by importing the copied file with the vendored runtime on the
        path, which is exactly what happens in a customer's package.
        """
        from app.runtime.export import build_export, write_export
        from tests.test_export_agent import APP_ROOT, _graph

        root = tmp_path / "agent"
        write_export(build_export(_graph(), APP_ROOT), root)

        example = root / "tools" / "lookup_order.py.example"
        module = root / "tools" / "lookup_order.py"
        module.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

        runtime_path = root / "vendor" / "runtime"
        result = subprocess.run(
            [
                PYTHON,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]);"
                "from app.tools.registry import get_tool_registry;"
                "import lookup_order;"
                "print(sorted(t.name for t in get_tool_registry().list()))",
                str(runtime_path),
                str(root / "tools"),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "lookup_order" in result.stdout, result.stdout
