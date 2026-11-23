"""Tests for the small defects fixed in one pass.

Each class names the defect it pins, because the value of these tests is that they
stop a specific thing from coming back — not that they cover a module.

  - `BUDGET_EXIT_BEHAVIOR=keep` reports a partial result; `error` does not.
  - The AI message tables cannot ship in one language.
  - The three AI endpoints all accept `lang`.
  - `smoke.py` and `dev_run.ps1` no longer default to the unusable port.
  - Single-node debugging accepts the node the canvas is editing, saved or not.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from app.ai_buildtime.gen_node import _MESSAGES as GEN_NODE_MESSAGES
from app.ai_buildtime.gen_node import fill_node
from app.runtime.ai.messages import missing_translations, render
from app.ai_buildtime.repair import _MESSAGES as REPAIR_MESSAGES
from app.graph.schema import (
    InputPort,
    NodeCategory,
    NodeTypeSpec,
    NumberParam,
    OutputPort,
    PortType,
    RunCreateRequest,
    RunStatus,
    WorkflowGraph,
)
from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import register_node
from app.runtime.budget import Budget
from app.runtime.manager import RunManager
from app.settings import Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# The one behaviour added this pass
# ---------------------------------------------------------------------------

DELAY_SPEC = NodeTypeSpec(
    type="defect_delay",
    label="Defect delay",
    category=NodeCategory.DATA,
    inputs=[InputPort(id="in", label="In", type=PortType.ANY)],
    outputs=[OutputPort(id="out", label="Out", type=PortType.ANY)],
    params=[NumberParam(name="seconds", label="Seconds", default=0.05)],
)


@register_node
class DefectDelayNode(BaseNode):
    spec = DELAY_SPEC

    async def run(self, ctx: NodeContext) -> dict:
        await asyncio.sleep(float(ctx.config.get("seconds") or 0.05))
        return {"out": ctx.input("in")}


def _node(node_id: str, node_type: str, **config):
    return {"id": node_id, "type": node_type, "label": node_id, "config": config}


def _edge(source: str, target: str):
    return {
        "id": f"{source}->{target}",
        "source": source,
        "sourceHandle": "out",
        "target": target,
        "targetHandle": "in",
    }


def _graph(workflow_id: str) -> WorkflowGraph:
    return WorkflowGraph(
        id=workflow_id,
        nodes=[
            _node("start", "start", input_schema={}),
            _node("a", "defect_delay", seconds=0.01),
            _node("b", "defect_delay", seconds=0.01),
            _node("end", "end", output_label="result"),
        ],
        edges=[_edge("start", "a"), _edge("a", "b"), _edge("b", "end")],
    )


async def _stopped(manager: RunManager, graph: WorkflowGraph, *, keep: bool):
    context = await manager.create(
        graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=50)
    )
    context.budget.max_steps = 2
    context.budget.exit_behavior = "keep" if keep else "error"
    await manager.start(context)
    assert context.task is not None
    await context.task
    return context


class TestABudgetStopCanKeepWhatItProduced:
    async def test_error_exit_reports_nothing(self):
        context = await _stopped(RunManager(), _graph("wf-exit-error"), keep=False)
        assert context.status is RunStatus.FAILED
        assert context.outputs == {}

    async def test_keep_exit_reports_the_partial_state(self):
        """The point of stopping is to avoid more spend, not to discard the spend
        that already happened."""
        context = await _stopped(RunManager(), _graph("wf-exit-keep"), keep=True)
        assert context.status is RunStatus.FAILED, (
            "a stopped run did not complete, so it must not report success even "
            "when it preserves its partial result"
        )
        assert context.outputs.get("partial") is True
        assert "a" in (context.outputs.get("vars") or {}), (
            "the node that did run has no output in the partial result"
        )

    def test_the_two_values_are_the_only_ones_documented(self):
        assert Budget(exit_behavior="error").keeps_partial_result is False
        assert Budget(exit_behavior="keep").keeps_partial_result is True

    def test_the_setting_reaches_the_budget(self):
        assert Budget.from_settings(_settings(budget_exit_behavior="keep")).exit_behavior == "keep"
        assert Budget.from_settings(_settings()).exit_behavior == "error"


# ---------------------------------------------------------------------------
# AI message tables
# ---------------------------------------------------------------------------


class TestTheAiMessagesCannotShipInOneLanguage:
    def test_every_table_entry_has_both_languages(self):
        for name, table in (("repair", REPAIR_MESSAGES), ("gen_node", GEN_NODE_MESSAGES)):
            missing = missing_translations(table)
            assert not missing, f"{name} has untranslated message(s): {missing}"

    def test_an_unknown_language_falls_back_to_english(self):
        table = {"k": {"en": "English", "zh": "中文"}}
        assert render(table, "k", "de") == "English"
        assert render(table, "k", "zh") == "中文"

    def test_substitution_happens(self):
        table = {"k": {"en": "saw {n}", "zh": "看到 {n}"}}
        assert render(table, "k", "en", n=3) == "saw 3"

    def test_an_unknown_key_raises_rather_than_printing_the_key(self):
        """A missing message is a programming error; showing a user `some_key`
        would hide it."""
        with pytest.raises(KeyError):
            render({"k": {"en": "x"}}, "nope", "en")


class TestTheThreeAiEndpointsAgreeOnLanguage:
    def test_all_three_request_models_accept_lang(self):
        import app.api.ai as ai

        for name in ("FillNodeRequest", "GenerateGraphRequest", "RepairGraphRequest"):
            model = getattr(ai, name)
            assert "lang" in model.model_fields, f"{name} does not accept a language"

    def test_fill_node_accepts_a_language_keyword(self):
        import inspect

        assert "lang" in inspect.signature(fill_node).parameters

    def test_generate_graph_accepts_a_language_keyword(self):
        import inspect

        from app.ai_buildtime.gen_graph import generate_graph

        assert "lang" in inspect.signature(generate_graph).parameters


# ---------------------------------------------------------------------------
# Single-node debugging
# ---------------------------------------------------------------------------


def _node_test_graph() -> dict:
    """One saved Prompt Template node, and nothing else."""
    return {
        "id": "wf-node-test",
        "name": "Node test probe",
        "nodes": [
            {
                "id": "tpl",
                "type": "prompt",
                "label": "Saved template",
                "config": {"template": "SAVED[{{ inputs.in }}]"},
            }
        ],
        "edges": [],
    }


@pytest.fixture
def node_test_client():
    """The real app, reached from loopback so `AUTH_MODE=local` lets it through."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, client=("127.0.0.1", 55556)) as test_client:
        yield test_client


class TestSingleNodeDebugAcceptsAnUnsavedNode:
    """The node under the cursor is the one being tested, saved or not.

    Measured (2026-10-08, canvas browser test): adding a Prompt Template node and
    pressing "Run node" answered HTTP 404 `No node 'prompt_36nykq'.` — the endpoint
    resolved the node out of the *stored* workflow, so nothing the user had not
    saved could be run. That includes the config edit, which is the case the panel
    exists for.
    """

    def test_a_node_that_is_not_in_the_stored_workflow_runs(self, node_test_client):
        assert node_test_client.post("/api/workflows", json=_node_test_graph()).status_code == 201

        response = node_test_client.post(
            "/api/runs/node-test",
            json={
                "workflow_id": "wf-node-test",
                "node_id": "tpl-new",
                "node": {
                    "id": "tpl-new",
                    "type": "prompt",
                    "label": "Unsaved template",
                    # The whole shape the canvas holds, so a schema tightening
                    # cannot break the panel without failing here first.
                    "description": "",
                    "position": {"x": 10, "y": 20},
                    "config": {"template": "Hello {{ inputs.in }}"},
                    "ui": {},
                    "trusted": False,
                },
                "inputs": {"in": "world"},
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True, body
        assert body["outputs"]["out"] == "Hello world"

    def test_the_supplied_node_beats_the_stored_copy(self, node_test_client):
        """The whole point: an edit that has not been saved is what gets run."""
        assert node_test_client.post("/api/workflows", json=_node_test_graph()).status_code == 201

        response = node_test_client.post(
            "/api/runs/node-test",
            json={
                "workflow_id": "wf-node-test",
                "node_id": "tpl",
                "node": {
                    "id": "tpl",
                    "type": "prompt",
                    "config": {"template": "EDITED[{{ inputs.in }}]"},
                },
                "inputs": {"in": "x"},
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["outputs"]["out"] == "EDITED[x]"

    def test_the_id_only_path_still_reads_the_stored_node(self, node_test_client):
        """A caller with nothing but an id — a script, a test — must keep working."""
        assert node_test_client.post("/api/workflows", json=_node_test_graph()).status_code == 201

        response = node_test_client.post(
            "/api/runs/node-test",
            json={"workflow_id": "wf-node-test", "node_id": "tpl", "inputs": {"in": "x"}},
        )

        assert response.status_code == 200, response.text
        assert response.json()["outputs"]["out"] == "SAVED[x]"

    def test_a_node_that_is_in_neither_place_is_still_a_404(self, node_test_client):
        assert node_test_client.post("/api/workflows", json=_node_test_graph()).status_code == 201

        response = node_test_client.post(
            "/api/runs/node-test",
            json={"workflow_id": "wf-node-test", "node_id": "nope", "inputs": {}},
        )

        assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


class TestTheScriptsNoLongerUseTheUnusablePort:
    def test_smoke_defaults_to_the_working_port(self):
        text = (ROOT / "scripts" / "smoke.py").read_text(encoding="utf-8")
        assert 'BASE = "http://127.0.0.1:8001"' in text
        assert "8000" not in text, "smoke.py still mentions the zombie port"

    def test_dev_run_resolves_the_port_instead_of_hardcoding_it(self):
        text = (ROOT / "scripts" / "dev_run.ps1").read_text(encoding="utf-8")
        assert "$env:BACKEND_PORT" in text, "dev_run.ps1 does not read BACKEND_PORT"
        assert re.search(r"-port',?\s*\"\$Port\"", text) or '"$Port"' in text, (
            "dev_run.ps1 still starts uvicorn on a literal port"
        )
        assert "foreach ($port in $Port, $FrontendPort)" in text, (
            "dev_run.ps1 clears the wrong ports"
        )
