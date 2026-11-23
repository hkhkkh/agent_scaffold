"""Phase 1 capability layer — the tool registry.

What these tests are actually defending
---------------------------------------

The valuable assertion is not "the registry returns tools" — Phase 0 already
covered that. It is **"the node asks the capability layer, and a deployment can
bound the answer"**. A node that imports the module-level function directly
still passes every existing test while making the abstraction decorative; that
is the failure this project has now hit three times (§13.11), and
``_guardprobe_capability.py`` breaks the wiring to check it does not happen a
fourth.

The second half of the file covers a freeze that existed before the interface
did. ``nodes/tool.py`` built its dropdown, and ``ai/gen_graph.py`` built its
prompt, from ``list_tools()`` **at import time**. Both were module constants, so
they recorded the tool set as it was when the process started. Harmless while
there was exactly one possible tool set; a real bug the moment a deployment can
configure one, because the UI would offer a tool the agent does not have and the
user would only find out when the run failed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.settings import Settings, get_settings
from app.tools.registry import (
    BuiltinToolRegistry,
    ScopedToolRegistry,
    ToolDef,
    ToolError,
    ToolRegistry,
    build_tool_registry,
    get_tool_registry,
    list_tools,
    set_tool_registry,
    unknown_tool_names,
)


@pytest.fixture(autouse=True)
def _restore_registry():
    """The registry and the settings cache are process-wide.

    Both must be restored, not just the registry: a test that monkeypatches
    ``TOOLS_ALLOWLIST`` leaves the parsed settings cached, and every later test
    would then build its registry from the wrong configuration.
    """
    yield
    set_tool_registry(None)
    get_settings.cache_clear()


def _settings(**overrides: Any) -> Settings:
    """Deterministic settings: never read the developer's `.env`."""
    return Settings(_env_file=None, **overrides)


def _fake_tool(name: str, label: str = "") -> ToolDef:
    return ToolDef(
        name=name,
        label=label or name.title(),
        description=f"Test tool {name}.",
        parameters={"type": "object", "properties": {}},
    )


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class TestTheInterfaceIsCoherent:
    def test_both_implementations_satisfy_it(self):
        for registry in (BuiltinToolRegistry(), ScopedToolRegistry(BuiltinToolRegistry())):
            assert isinstance(registry, ToolRegistry)

    def test_an_unknown_tool_raises_rather_than_returning_none(self):
        """Returning None would let a caller mistake absence for a result."""
        with pytest.raises(ToolError):
            get_tool_registry().get("agent-scaffold-no-such-tool")

    def test_listing_is_stable(self):
        """The list feeds a dropdown and a prompt; reshuffling is a real cost."""
        registry = get_tool_registry()
        assert registry.names() == registry.names()
        assert registry.names() == sorted(registry.names(), key=lambda n: registry.get(n).label)

    def test_the_free_functions_delegate(self):
        """Otherwise the module namespace is a second path around the interface.

        Swapping the registry must change what ``list_tools()`` returns too —
        otherwise a caller that kept the old import silently opts out of the
        deployment's configuration.
        """
        set_tool_registry(ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"})))

        assert [tool.name for tool in list_tools()] == ["calculator"]

    def test_contains_does_not_raise(self):
        registry = get_tool_registry()
        assert "calculator" in registry
        assert "agent-scaffold-no-such-tool" not in registry


# ---------------------------------------------------------------------------
# Bounding an agent
# ---------------------------------------------------------------------------


class TestScopedRegistry:
    def _scoped(self, **kwargs: Any) -> ScopedToolRegistry:
        return ScopedToolRegistry(BuiltinToolRegistry(), **kwargs)

    def test_allowlist_bounds_the_list(self):
        registry = self._scoped(allow=frozenset({"calculator", "current_time"}))
        assert registry.names() == ["calculator", "current_time"]

    def test_an_empty_allowlist_means_everything(self):
        """The zero-configuration case must survive being scoped."""
        registry = self._scoped()
        assert len(registry.list()) == len(BuiltinToolRegistry().list())

    def test_denylist_switches_off_one_tool_without_enumerating_the_rest(self):
        registry = self._scoped(deny=frozenset({"http_request"}))

        assert "http_request" not in registry.names()
        assert "calculator" in registry.names()

    def test_deny_wins_over_allow(self):
        """Otherwise a deny entry could be defeated by also allowing it."""
        registry = self._scoped(
            allow=frozenset({"http_request"}), deny=frozenset({"http_request"})
        )
        assert registry.names() == []

    def test_a_registered_but_disallowed_tool_is_a_different_error(self):
        """The distinction matters to whoever is reading the log.

        "Unknown tool" sends an operator hunting for a typo. "Not enabled" tells
        them it is a policy. Collapsing the two costs debugging time on exactly
        the configuration this feature exists to make visible.
        """
        registry = self._scoped(allow=frozenset({"calculator"}))

        with pytest.raises(ToolError, match="not enabled for this agent"):
            registry.get("http_request")

        with pytest.raises(ToolError, match="Unknown tool"):
            registry.get("agent-scaffold-no-such-tool")

    def test_schemas_respect_the_scope(self):
        """What the model is offered must match what it may call.

        Handing the model a schema for a tool the registry will refuse produces a
        failed tool call the agent cannot recover from — it has no way to know
        the tool was never available.
        """
        registry = self._scoped(allow=frozenset({"calculator"}))

        assert [s["function"]["name"] for s in registry.schemas()] == ["calculator"]

    def test_registering_forwards_to_the_inner_registry(self):
        """A view does not own its contents.

        Raising here instead would turn a plugin that registers late into a
        startup failure, which is a worse trade than a tool that is registered
        but not visible (the allowlist's decision, and the correct one).

        Uses an isolated store rather than the module one: registering into the
        shared dict would leak `probe_tool` into every test that runs afterwards,
        which is exactly the kind of order-dependence that makes a suite lie.
        """
        inner = BuiltinToolRegistry(store={})
        registry = ScopedToolRegistry(inner, allow=frozenset({"calculator"}))

        registry.register(_fake_tool("probe_tool"))

        assert "probe_tool" in inner.names()
        assert "probe_tool" not in registry.names()

    def test_an_isolated_store_does_not_touch_the_process_one(self):
        """The injectable store is what makes the test above honest."""
        isolated = BuiltinToolRegistry(store={})
        isolated.register(_fake_tool("probe_tool"))

        assert "probe_tool" not in BuiltinToolRegistry().names()


class TestBuildingFromConfiguration:
    def test_no_configuration_gives_the_plain_registry(self):
        """Not an inert filter — the default path has no extra layer at all."""
        assert isinstance(build_tool_registry(_settings()), BuiltinToolRegistry)

    def test_an_allowlist_produces_a_scoped_registry(self):
        registry = build_tool_registry(
            _settings(tools_allowlist=["calculator", "current_time"])
        )
        assert isinstance(registry, ScopedToolRegistry)
        assert registry.names() == ["calculator", "current_time"]

    def test_a_denylist_produces_a_scoped_registry(self):
        registry = build_tool_registry(_settings(tools_denylist=["http_request"]))
        assert isinstance(registry, ScopedToolRegistry)
        assert "http_request" not in registry.names()

    def test_public_demo_forces_destructive_database_tools_out(self):
        registry = build_tool_registry(_settings(public_demo=True))
        for name in ("db_reset", "db_update", "db_delete"):
            assert name not in registry.names()
        assert "db_query" in registry.names()

    def test_a_comma_separated_env_value_is_split(self, monkeypatch):
        """`.env` stays readable: TOOLS_ALLOWLIST=a,b rather than JSON."""
        monkeypatch.setenv("TOOLS_ALLOWLIST", "calculator, current_time")
        registry = build_tool_registry(_settings())

        assert registry.names() == ["calculator", "current_time"]

    def test_blank_entries_are_ignored(self):
        registry = build_tool_registry(_settings(tools_allowlist=["calculator", " ", ""]))
        assert registry.names() == ["calculator"]

    def test_unknown_names_are_reported(self):
        """The typo case, which fails *safe* but breaks the agent.

        `TOOLS_ALLOWLIST=calclator` yields an agent with no tools. Nothing errors;
        the agent simply cannot do anything. Without a startup check the operator
        has no signal at all.

        Note the check must consult the *base* registry: asking the scoped one
        whether `calclator` is known returns yes, because the allowlist is what
        said so.
        """
        build_tool_registry(_settings(tools_allowlist=["calclator"]))

        assert unknown_tool_names(["calclator"]) == ["calclator"]
        assert unknown_tool_names(["calculator"]) == []
        assert unknown_tool_names(["calclator", "calculator", "nope"]) == [
            "calclator",
            "nope",
        ]


# ---------------------------------------------------------------------------
# The capability is live
# ---------------------------------------------------------------------------


class TestNodesGoThroughTheCapabilityLayer:
    """Not "tools work" — "a deployment can change which tools a node gets"."""

    def test_the_tool_node_cannot_call_a_tool_outside_the_scope(self):
        from app.graph.schema import NodeSpec
        from app.nodes.base import NodeContext
        from app.nodes.tool import ToolNode

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )
        context = _node_context(
            NodeSpec(id="n1", type="tool", label="Tool"),
            _tool_config("http_request"),
        )

        with pytest.raises(ValueError, match="not enabled for this agent"):
            asyncio.run(ToolNode(context.node).run(context))

    def test_the_tool_node_still_runs_an_allowed_tool(self):
        """A scope that breaks the feature is not a feature."""
        from app.graph.schema import NodeSpec
        from app.nodes.tool import ToolNode

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )
        context = _node_context(
            NodeSpec(id="n1", type="tool", label="Tool"),
            _tool_config("calculator", arguments={"expression": "6*7"}),
        )

        output = asyncio.run(ToolNode(context.node).run(context))

        assert output["out"]["result"] == 42

    def test_the_tool_node_honours_fail_on_error_for_an_unavailable_tool(self):
        """`fail_on_error` used to be bypassed for exactly this failure.

        The tool was looked up *before* the try block, so "not enabled" escaped
        as a raw error while every other tool failure respected the switch. An
        inconsistency nobody would predict from the Inspector.
        """
        from app.graph.schema import NodeSpec
        from app.nodes.tool import ToolNode

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )
        context = _node_context(
            NodeSpec(id="n1", type="tool", label="Tool"),
            _tool_config("http_request", fail_on_error=False),
        )

        output = asyncio.run(ToolNode(context.node).run(context))

        assert "not enabled for this agent" in output["out"]["error"]

    def test_the_agent_node_offers_only_the_scoped_tools(self):
        """`tools: []` must mean "everything this agent is allowed", not "everything".

        The difference is invisible with one global registry and is the whole
        point of scoping.
        """
        from app.nodes.agent import _select_tools

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )

        assert [tool.name for tool in _select_tools([])] == ["calculator"]

    def test_the_agent_node_reports_a_scoped_away_tool_as_a_config_error(self):
        """`_select_tools` raises the domain error; the node adds context.

        The node is what a user actually reaches, so the conversion to the
        node-level error is asserted against it rather than against the helper.
        Selection fails before any model call, so this needs no model.
        """
        from app.graph.schema import NodeSpec
        from app.nodes.agent import AgentNode, _select_tools

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )

        with pytest.raises(ToolError, match="not enabled for this agent"):
            _select_tools(["http_request"])

        node = NodeSpec(
            id="n1",
            type="agent",
            label="Agent",
            config={"task": "do something", "tools": ["http_request"]},
        )
        context = _node_context(node, dict(node.config))

        with pytest.raises(ValueError, match="not enabled for this agent"):
            asyncio.run(AgentNode(node).run(context))


# ---------------------------------------------------------------------------
# The list is no longer frozen at import
# ---------------------------------------------------------------------------


class TestNothingIsFrozenAtImport:
    """The interface is only real if the UI and the prompt follow it."""

    def test_the_tool_node_dropdown_follows_the_registry(self):
        from app.nodes import get_node_type_spec
        from app.nodes.registry import refresh_dynamic_specs

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )
        refresh_dynamic_specs()

        spec = get_node_type_spec("tool")
        param = next(p for p in spec.params if p.name == "tool")

        assert [option.value for option in param.options] == ["calculator"]

    def test_the_dropdown_default_never_points_at_a_missing_tool(self):
        """A default naming a tool that is not offered would be silently wrong."""
        from app.nodes import get_node_type_spec
        from app.nodes.registry import refresh_dynamic_specs

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"current_time"}))
        )
        refresh_dynamic_specs()

        spec = get_node_type_spec("tool")
        param = next(p for p in spec.params if p.name == "tool")

        assert param.default == "current_time"

    def test_an_empty_scope_yields_no_options_and_no_dangling_default(self):
        from app.nodes import get_node_type_spec
        from app.nodes.registry import refresh_dynamic_specs

        set_tool_registry(ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"nonexistent"})))
        refresh_dynamic_specs()

        spec = get_node_type_spec("tool")
        param = next(p for p in spec.params if p.name == "tool")

        assert param.options == []
        assert param.default == ""

    def test_the_ai_prompt_lists_the_scoped_tools(self):
        """Otherwise the model is told about a tool that will refuse it."""
        from app.ai_buildtime.gen_graph import _tool_list

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )

        assert _tool_list() == "calculator"

    def test_the_catalogue_refreshes_before_serving(self):
        """The endpoint is where the palette's data comes from.

        Asserted against the real endpoint rather than the helper, because the
        bug this guards against is "the refresh exists but nobody calls it" —
        the same shape as the uninstalled auth middleware in §13.11.
        """
        from fastapi.testclient import TestClient

        from app.main import app

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )

        with TestClient(app, client=("127.0.0.1", 51234)) as client:
            body = client.get("/api/node-types").json()

        tool_type = next(t for t in body["types"] if t["type"] == "tool")
        param = next(p for p in tool_type["params"] if p["name"] == "tool")
        assert [option["value"] for option in param["options"]] == ["calculator"]

    def test_the_tools_endpoint_respects_the_scope(self):
        from fastapi.testclient import TestClient

        from app.main import app

        set_tool_registry(
            ScopedToolRegistry(BuiltinToolRegistry(), allow=frozenset({"calculator"}))
        )

        with TestClient(app, client=("127.0.0.1", 51234)) as client:
            body = client.get("/api/tools").json()

        assert [tool["name"] for tool in body["tools"]] == ["calculator"]

    def test_the_default_deployment_is_unchanged(self):
        """The regression that matters most: exactly the built-ins, no more.

        Written as an exact set so that adding a tool is a deliberate edit here
        rather than a silent widening. The failure this guards is the palette
        being frozen at import or picking up a plugin's tools — not a built-in
        being added on purpose, which is what ``web_search`` was.

        The four ``db_*`` tools are the second such deliberate addition
        (2026-10-16). Without them a workflow cannot record anything durable:
        the Code node's sandbox removes ``open`` and its module allowlist has no
        ``sqlite3``, so there was no other seam to put a write behind.

        The ten files tools are the third (2026-11-03): they arrive through the
        **files capability pack**, not through ``app.tools.builtin``, and the
        platform registers every built-in pack in the app lifespan. That is why
        this test asks for the registration explicitly instead of relying on a
        previous test having created a ``TestClient`` — the palette must be the
        same whether or not some earlier test happened to start the app.
        """
        from app.nodes import get_node_type_spec
        from app.nodes.registry import refresh_dynamic_specs
        from app.tools import register_capability_pack_tools

        register_capability_pack_tools()
        refresh_dynamic_specs()
        spec = get_node_type_spec("tool")
        param = next(p for p in spec.params if p.name == "tool")

        assert {option.value for option in param.options} == {
            # core pack
            "http_request",
            "calculator",
            "current_time",
            "json_query",
            "text_stats",
            "web_search",
            "db_write",
            "db_update",
            "db_delete",
            "db_query",
            "db_tables",
            "db_reset",
            # files pack
            "file_list",
            "file_read",
            "file_write",
            "csv_read",
            "csv_write",
            "excel_read",
            "excel_write",
            "pdf_read",
            "docx_read",
            "docx_write",
        }


def _node_context(node: Any, config: dict[str, Any]) -> Any:
    """Minimal node context.

    ``config`` is required rather than defaulted: the Tool and Agent nodes read
    different keys, and a helper that guesses produces a context that quietly
    belongs to the other node — which is how the first version of this file
    managed to test the Agent node with the Tool node's config.
    """
    from app.nodes.base import NodeContext

    async def emit(_payload: dict[str, Any]) -> None:
        return None

    async def log(_text: str, _level: str = "info") -> None:
        return None

    node.config = config
    return NodeContext(
        node=node,
        config=config,
        inputs={"in": None},
        state={"vars": {}},
        settings=_settings(),
        emit=emit,
        log=log,
    )


def _tool_config(name: str, **overrides: Any) -> dict[str, Any]:
    return {"tool": name, "arguments": {}, "fail_on_error": True, **overrides}


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


class TestStartupValidation:
    """The check has to be *called*, not merely correct.

    A typo'd ``TOOLS_ALLOWLIST`` fails safe and silently: the agent gets no
    tools and nothing errors. Validating the names is only useful if startup
    actually reports them, which is why this asserts against the real application
    rather than against the function (the lesson from §13.11).
    """

    def test_startup_warns_about_an_unknown_tool_name(self, monkeypatch, caplog):
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setenv("TOOLS_ALLOWLIST", "calculator,calclator")
        get_settings.cache_clear()
        set_tool_registry(None)  # rebuild from the patched settings

        with caplog.at_level(logging.WARNING, logger="app"):
            with TestClient(app, client=("127.0.0.1", 51234)):
                pass

        assert "calclator" in caplog.text
        assert "TOOLS_ALLOWLIST" in caplog.text

    def test_startup_is_quiet_when_the_configuration_is_clean(self, monkeypatch, caplog):
        """A warning for every valid start would be noise, and noise is ignored."""
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setenv("TOOLS_ALLOWLIST", "calculator")
        get_settings.cache_clear()
        set_tool_registry(None)

        with caplog.at_level(logging.WARNING, logger="app"):
            with TestClient(app, client=("127.0.0.1", 51234)):
                pass

        assert "unrecognised tool" not in caplog.text

    def test_startup_reports_which_tools_this_deployment_has(self, monkeypatch, caplog):
        """The allowlist bounds the agent, so the log must show the effect."""
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setenv("TOOLS_ALLOWLIST", "calculator")
        get_settings.cache_clear()
        set_tool_registry(None)

        with caplog.at_level(logging.INFO, logger="app"):
            with TestClient(app, client=("127.0.0.1", 51234)):
                pass

        assert "Tools        : calculator" in caplog.text
