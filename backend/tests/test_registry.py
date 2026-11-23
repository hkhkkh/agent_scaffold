"""Tests for the node registry and tool registry."""

from __future__ import annotations

import pytest

import app.nodes as nodes
from app.tools import list_tools


class TestNodeRegistry:
    def test_builtin_node_types_are_registered(self):
        specs = {s.type for s in nodes.list_node_type_specs()}
        expected = {
            "start",
            "end",
            "llm",
            "prompt",
            "condition",
            "code",
            "agent",
            "tool",
            "retriever",
            "human_input",
            "loop",
            "aggregator",
        }
        assert expected <= specs, f"missing: {expected - specs}"

    def test_every_type_has_exactly_one_entry_point(self):
        entries = [s for s in nodes.list_node_type_specs() if s.is_entry]
        assert len(entries) == 1
        assert entries[0].type == "start"

    def test_every_type_has_at_least_one_terminal(self):
        terminals = [s for s in nodes.list_node_type_specs() if s.is_terminal]
        assert terminals, "expected at least one terminal node type"

    def test_param_names_are_unique_within_a_type(self):
        for spec in nodes.list_node_type_specs():
            names = [p.name for p in spec.params]
            assert len(names) == len(set(names)), f"{spec.type} has duplicate params"

    def test_port_ids_are_unique_within_a_type(self):
        for spec in nodes.list_node_type_specs():
            ids = [p.id for p in spec.inputs] + [p.id for p in spec.outputs]
            assert len(ids) == len(set(ids)), f"{spec.type} has duplicate port ids"

    def test_code_node_requires_trust(self):
        spec = nodes.get_node_type_spec("code")
        assert spec.requires_trust is True

    def test_unknown_type_raises(self):
        with pytest.raises(nodes.NodeTypeError):
            nodes.get_node_type_spec("does_not_exist")

    def test_params_serialize_with_discriminator(self):
        """The frontend switches on `type`, so it must survive serialization."""
        spec = nodes.get_node_type_spec("llm")
        payload = spec.model_dump(mode="json")
        kinds = {p["type"] for p in payload["params"]}
        assert "string" in kinds
        assert all(isinstance(p["type"], str) for p in payload["params"])


class TestDynamicPorts:
    def test_condition_exposes_one_handle_per_branch(self):
        from app.graph.schema import NodeSpec

        node = NodeSpec(
            id="c1",
            type="condition",
            config={
                "branches": [
                    {"id": "yes", "label": "Yes", "expression": "True"},
                    {"id": "no", "label": "No", "expression": ""},
                ]
            },
        )
        ports = nodes.effective_outputs(node)
        assert [p.id for p in ports] == ["yes", "no"]

    def test_unconfigured_condition_falls_back_to_declared_ports(self):
        from app.graph.schema import NodeSpec

        node = NodeSpec(id="c1", type="condition", config={"branches": []})
        ports = nodes.effective_outputs(node)
        assert ports, "an unconfigured condition must still render handles"

    def test_static_node_ignores_dynamic_logic(self):
        from app.graph.schema import NodeSpec

        node = NodeSpec(id="l1", type="llm")
        assert [p.id for p in nodes.effective_outputs(node)] == ["out", "message"]


class TestToolRegistry:
    def test_builtin_tools_are_registered(self):
        names = {t.name for t in list_tools()}
        assert {"http_request", "calculator", "current_time", "json_query", "text_stats"} <= names

    def test_every_tool_has_a_valid_openai_schema(self):
        for tool in list_tools():
            schema = tool.openai_schema()
            assert schema["type"] == "function"
            assert schema["function"]["name"] == tool.name
            assert schema["function"]["description"]
            assert schema["function"]["parameters"]["type"] == "object"

    def test_http_tool_is_flagged_dangerous(self):
        from app.tools import get_tool

        assert get_tool("http_request").dangerous is True

    def test_unknown_tool_raises(self):
        from app.tools import ToolError, get_tool

        with pytest.raises(ToolError):
            get_tool("nope")


class TestDefaultConfig:
    def test_defaults_are_collected_from_params(self):
        cls = nodes.get_node_class("llm")
        defaults = cls.defaults_for(nodes.get_node_type_spec("llm"))
        assert "prompt" in defaults
        assert defaults["output_format"] == "text"
