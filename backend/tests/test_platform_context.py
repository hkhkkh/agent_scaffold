"""The canvas AI must know how the platform runs what it draws.

These tests pin the facts that used to be missing from every build-time prompt:
one run is bounded, repetition is a schedule, loops are for finite iteration,
there is no wait node, and exported packages use a daemon. They also pin the
copilot's platform tools, because "the AI knows" is only true if it can read the
live answer and create a *paused* trigger rather than claiming one exists.
"""

from __future__ import annotations

import pytest

from app.ai_buildtime.platform_brief import (
    deployment_plan,
    platform_brief,
    workflow_context,
)
from app.ai_buildtime.platform_tools import (
    create_schedule,
    dispatch_platform_tool,
    set_schedule_enabled,
)
from app.ai_buildtime.prompts import (
    build_copilot_edit_messages,
    build_copilot_insight_messages,
    build_generate_graph_messages,
)
from app.db.repository import save_workflow
from app.graph.schema import WorkflowGraph
from app.scheduling import store


def _graph(workflow_id: str = "wf-platform") -> WorkflowGraph:
    return WorkflowGraph(
        id=workflow_id,
        name="Platform probe",
        nodes=[
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "config": {"input_schema": {"query": "hello"}},
            },
            {"id": "end", "type": "end", "label": "End", "config": {}},
        ],
        edges=[
            {
                "id": "s->e",
                "source": "start",
                "sourceHandle": "out",
                "target": "end",
                "targetHandle": "in",
            }
        ],
    )


class TestTheBriefIsLive:
    def test_it_names_the_bounded_run_and_the_real_limits(self):
        brief = platform_brief()

        assert "One run is bounded" in brief
        assert "recursion_limit" in brief
        assert "MIN_RUN" not in brief  # a placeholder would be worse than nothing

    def test_it_explains_schedules_loops_and_the_missing_wait_node(self):
        brief = platform_brief()

        assert "trigger on the workflow" in brief
        assert "minutely, hourly, daily, weekly, monthly" in brief
        assert "hard ceiling" in brief
        assert "no Wait/Delay node" in brief
        assert "Do not use a loop as an outer" in brief

    def test_it_knows_the_export_daemon_is_outside_the_graph(self):
        brief = platform_brief()

        assert "main.py daemon" in brief
        assert "deployment action, outside the graph" in brief

    def test_the_tone_is_a_tool_not_a_companion(self):
        system = build_copilot_insight_messages(_graph(), "你好")[0]["content"]

        assert "workflow engineering assistant" in system
        assert "general conversational assistant" in system
        assert "no greetings" in system
        assert "state it\n  as a decision" in system

    async def test_build_time_calls_force_zero_temperature(self, monkeypatch):
        from app.ai_buildtime import gen_node

        captured: dict = {}

        def _model(overrides, **kwargs):
            captured.update(overrides)
            return type("M", (), {"model_name": "mock-model"})()

        monkeypatch.setattr(gen_node, "build_chat_model", _model)
        with pytest.raises(Exception, match="No LLM API key"):
            await gen_node._complete_message([{"role": "user", "content": "x"}])
        assert captured["temperature"] == 0.0

    def test_it_reflects_the_public_demo_posture(self, monkeypatch):
        from app.settings import get_settings

        monkeypatch.setenv("PUBLIC_DEMO", "true")
        monkeypatch.setenv("PUBLIC_ACCESS", "true")
        monkeypatch.setenv("CODE_NODE_ENABLED", "false")
        monkeypatch.setenv("SCHEDULER_ENABLED", "false")
        get_settings.cache_clear()

        brief = platform_brief()

        assert "Code nodes are DISABLED" in brief
        assert "scheduling is DISABLED" in brief
        assert "public demo is on" in brief


class TestPromptsCarryTheBrief:
    def test_generation_and_copilot_see_it(self):
        graph = _graph()
        insight = build_copilot_insight_messages(graph, "让它持续监控")
        edit = build_copilot_edit_messages(graph, "让它持续监控", {}, {})
        generate = build_generate_graph_messages("建一个持续监控的 agent")

        for messages in (insight, edit, generate):
            system = messages[0]["content"]
            assert "Platform facts" in system
            assert "continuous" in system.lower()


class TestWorkflowTriggersAreReadable:
    def test_context_reports_existing_triggers(self):
        graph = _graph()
        save_workflow(graph)
        row = store.create_schedule(
            workflow_id=graph.id,
            rule={"frequency": "minutely", "interval": 1},
            name="every minute",
            enabled=True,
        )

        text = workflow_context(graph.id)

        assert "every minute" in text
        assert row["id"] in text
        assert "next fire" in text

    def test_context_says_so_when_nothing_is_scheduled(self):
        save_workflow(_graph())

        assert "No triggers are registered" in workflow_context("wf-platform")

    def test_the_insight_prompt_sees_them_too(self):
        graph = _graph()
        save_workflow(graph)
        store.create_schedule(
            workflow_id=graph.id,
            rule={"frequency": "minutely", "interval": 1},
            name="every minute",
            enabled=True,
        )

        messages = build_copilot_insight_messages(graph, "它现在在跑吗？")

        assert "Triggers (live)" in messages[1]["content"]
        assert "minutely" in messages[1]["content"]


class TestCopilotCanCreateAPausedTrigger:
    def test_it_creates_it_paused_and_reports_that(self):
        graph = _graph()
        save_workflow(graph)

        text = create_schedule(
            graph.id, frequency="minutely", inputs={"query": "hello"}
        )

        assert "PAUSED" in text
        rows = store.list_schedules()
        assert len(rows) == 1
        assert rows[0]["enabled"] is False
        assert rows[0]["workflow_id"] == graph.id
        assert rows[0]["inputs"] == {"query": "hello"}

    def test_it_rejects_an_input_the_start_node_does_not_declare(self):
        graph = _graph()
        save_workflow(graph)

        with pytest.raises(ValueError, match="not in the Start input_schema"):
            create_schedule(graph.id, frequency="minutely", inputs={"typo": 1})

    def test_it_rejects_an_invalid_rule(self):
        graph = _graph()
        save_workflow(graph)

        with pytest.raises(ValueError, match="invalid schedule rule"):
            create_schedule(graph.id, frequency="fortnightly")

    def test_public_demo_cannot_schedule(self, monkeypatch):
        graph = _graph()
        save_workflow(graph)
        from app.settings import get_settings

        monkeypatch.setenv("PUBLIC_DEMO", "true")
        monkeypatch.setenv("PUBLIC_ACCESS", "true")
        monkeypatch.setenv("CODE_NODE_ENABLED", "false")
        monkeypatch.setenv("SCHEDULER_ENABLED", "false")
        get_settings.cache_clear()

        with pytest.raises(ValueError, match="public demo"):
            create_schedule(graph.id, frequency="minutely")

    def test_it_can_enable_and_pause_its_own_trigger(self):
        graph = _graph()
        save_workflow(graph)
        row = store.create_schedule(
            workflow_id=graph.id, rule={"frequency": "minutely"}, enabled=False
        )

        enabled = set_schedule_enabled(graph.id, row["id"], True)
        assert "enabled" in enabled
        assert store.get_schedule(row["id"])["enabled"] is True

        paused = set_schedule_enabled(graph.id, row["id"], False)
        assert "paused" in paused
        assert store.get_schedule(row["id"])["enabled"] is False

    def test_it_cannot_touch_another_workflows_trigger(self):
        graph = _graph()
        other = _graph("wf-other")
        save_workflow(graph)
        save_workflow(other)
        row = store.create_schedule(
            workflow_id=other.id, rule={"frequency": "minutely"}, enabled=False
        )

        with pytest.raises(ValueError, match="different workflow"):
            set_schedule_enabled(graph.id, row["id"], True)

    def test_dispatch_returns_none_for_a_graph_tool(self):
        assert dispatch_platform_tool("add_node", {}, "wf-platform") is None


class TestDeploymentPlan:
    def test_it_returns_the_daemon_command_with_the_business_inputs(self):
        text = deployment_plan(
            "im07-line-guardian",
            interval=30,
            daily_at="08:00",
            timezone="Asia/Shanghai",
            inputs={"line_id": "IM-07", "mode": "monitor"},
            daily_inputs={"line_id": "IM-07", "mode": "daily_report"},
        )

        assert "main.py daemon --interval 30" in text
        assert '"line_id": "IM-07"' in text
        assert "--daily-at 08:00 --timezone Asia/Shanghai" in text
        assert "supervisor" in text

    def test_it_rejects_a_daily_time_without_daily_inputs(self):
        with pytest.raises(ValueError, match="daily_inputs"):
            deployment_plan("wf", daily_at="08:00")
