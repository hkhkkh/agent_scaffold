"""Writing a tool module from the canvas (step 0.5 of delivery).

The canvas had nowhere to put a business tool, which broke the delivery flow at
its first step and made the rest manual. These pin the two capabilities that fix
that — writing a module, and checking one — plus the fact that a checked module is
one the *exporter* can ship.

The checker runs the candidate in a subprocess, so a few of these are slow by
construction. That is the cost of not executing stranger's code inside the API
process, which is the same trade the code sandbox makes.
"""

from __future__ import annotations


import pytest

from app.tools.authoring import (
    ToolSourceError,
    check_module,
    tools_package_source,
    validate_module_name,
    validate_source,
)
from app.api import workflows
from app.tools.loading import load_tools_from

GOOD = '''"""A business tool."""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolDef, register_tool


async def _query_order(order_id: str) -> dict[str, Any]:
    return {"order_id": order_id}


register_tool(
    ToolDef(
        name="query_order",
        label="Query order",
        description="Look up an order.",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
        fn=_query_order,
    )
)
'''


class TestNameChecks:
    @pytest.mark.parametrize("name", ["", "1bad", "has space", "has-dash", "../escape"])
    def test_a_name_that_is_not_an_identifier_is_refused(self, name):
        """Also the path-traversal guard: the name becomes a filename."""
        with pytest.raises(ToolSourceError):
            validate_module_name(name)

    @pytest.mark.parametrize("name", ["_private", "main", "config", "vendor"])
    def test_reserved_names_are_refused(self, name):
        with pytest.raises(ToolSourceError, match="reserved"):
            validate_module_name(name)

    def test_a_good_name_is_returned(self):
        assert validate_module_name("query_order") == "query_order"

    def test_an_empty_module_is_refused(self):
        with pytest.raises(ToolSourceError, match="empty"):
            validate_source("   \n  ")


class TestCheckingAModule:
    def test_a_good_module_reports_the_tools_it_registers(self):
        """The point of returning names: the editor shows "defines: query_order"
        rather than asking the user to take it on trust."""
        report = check_module("query_order", GOOD)
        assert report["ok"] is True, report["error"]
        assert report["tools"] == ["query_order"]

    def test_a_syntax_error_is_reported_with_the_interpreter_message(self):
        report = check_module("broken", "def f(\n")
        assert report["ok"] is False
        assert "syntax" in (report["error"] or "").lower()

    def test_a_module_that_registers_nothing_is_reported(self):
        """Silent success would be the worst answer: it imports, it does nothing,
        and the agent simply never has the tool."""
        report = check_module("empty_tool", "x = 1\n")
        assert report["ok"] is False
        assert "registered no tools" in (report["error"] or "")

    def test_a_side_effect_at_import_time_is_caught_by_the_timeout(self, monkeypatch):
        """Import-time work is the usual reason a tool module is wrong, and the
        message says what to do about it."""
        from app import settings as settings_module

        monkeypatch.setattr(
            settings_module.Settings, "tool_check_seconds", 2.0, raising=False
        )
        settings_module.get_settings.cache_clear()
        try:
            report = check_module("slow", "import time\ntime.sleep(30)\n")
            assert report["ok"] is False
            assert "longer than" in (report["error"] or "")
        finally:
            settings_module.get_settings.cache_clear()

    def test_a_bad_module_does_not_raise(self):
        """A bad module is the expected input, not an exceptional condition."""
        report = check_module("whatever", "raise RuntimeError('boom')\n")
        assert report["ok"] is False
        assert "boom" in (report["error"] or "")

    def test_a_name_error_inside_the_module_is_reported(self):
        report = check_module(
            "oops",
            "from app.tools.registry import register_tool\nregister_tool(undefined_name)\n",
        )
        assert report["ok"] is False
        assert "undefined_name" in (report["error"] or "")


class TestWhatIsCheckedIsWhatShips:
    def test_the_checker_loads_through_the_same_loader_as_an_export(self, tmp_path):
        """A module that passes the check must be loadable in a package.

        Both go through `load_tools_from`, so this holds by construction — the
        assertion is here so that a future "faster" checker that greps instead of
        importing fails this test rather than the customer.
        """
        report = check_module("query_order", GOOD)
        assert report["ok"] is True

        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "__init__.py").write_text(
            tools_package_source({"query_order": GOOD}), encoding="utf-8"
        )
        (tools / "query_order.py").write_text(GOOD, encoding="utf-8")

        registry, problems = load_tools_from(tools)
        assert problems == []
        assert [tool.name for tool in registry.list()] == ["query_order"]

    def test_the_package_source_imports_every_module(self):
        source = tools_package_source({"a": "", "b": ""})
        assert "from . import a" in source
        assert "from . import b" in source

    def test_two_checker_runs_do_not_share_state(self):
        """The checker uses a temporary directory per run; a shared one would make
        the second check see the first module and report its tools."""
        first = check_module("one", GOOD.replace("query_order", "first_tool"))
        second = check_module("two", GOOD.replace("query_order", "second_tool"))
        assert first["tools"] == ["first_tool"]
        assert second["tools"] == ["second_tool"], (
            f"the second check saw the first module's tools: {second['tools']}"
        )


def _client():
    """The real router over a throwaway app, mounted the way `main` mounts it.

    A purpose-built app rather than the full application: the checker needs no
    database or model provider, and pulling those in would make a pure source
    check depend on unrelated startup work.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    # The router carries its own /workflows prefix, so the app adds only /api.
    app.include_router(workflows.router, prefix="/api")

    return TestClient(app, client=("127.0.0.1", 54321))



class TestSavingRefusesUnloadableModules:
    """Shape is checked on save; behaviour is checked on demand.

    The split matters because save runs on every autosave: importing a candidate
    module there would execute arbitrary code as the user types.
    """

    def test_a_reserved_module_name_is_refused_at_save_time(self):
        response = _client().put(
            "/api/workflows/w1",
            json={
                "id": "w1",
                "name": "t",
                "nodes": [],
                "edges": [],
                "tools": {"main": "x = 1\n"},
            },
        )
        assert response.status_code == 422, response.text
        assert "reserved" in response.json()["detail"]

    def test_an_empty_module_is_refused_at_save_time(self):
        response = _client().put(
            "/api/workflows/w1",
            json={
                "id": "w1",
                "name": "t",
                "nodes": [],
                "edges": [],
                "tools": {"good_name": "   "},
            },
        )
        assert response.status_code == 422, response.text

    def test_a_traversing_module_name_is_refused_at_save_time(self):
        """The name becomes a filename in an exported package."""
        response = _client().put(
            "/api/workflows/w1",
            json={
                "id": "w1",
                "name": "t",
                "nodes": [],
                "edges": [],
                "tools": {"../escape": "x = 1\n"},
            },
        )
        assert response.status_code == 422, response.text

    def test_a_broken_module_that_is_only_broken_at_run_time_still_saves(self):
        """Save must not be the behavioural gate, or every autosave of a
        half-written module would fail and the editor would lose work."""
        response = _client().put(
            "/api/workflows/w1",
            json={
                "id": "w1",
                "name": "t",
                "nodes": [],
                "edges": [],
                "tools": {"not_yet_right": "this is not python\n"},
            },
        )
        assert response.status_code != 422, response.text


class TestTheEndpoint:
    def test_check_returns_a_report_rather_than_raising(self):
        response = _client().post(
            "/api/workflows/tools/check", json={"name": "x", "source": "bad"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False

    def test_check_refuses_an_unusable_name_with_a_message(self):
        response = _client().post(
            "/api/workflows/tools/check",
            json={"name": "../escape", "source": GOOD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "module name" in body["error"]
