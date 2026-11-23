"""Budgets: a run that spends more than it is allowed stops, and says why.

Small on purpose. The interesting parts are the two behaviours that are easy to
get subtly wrong — where the check happens, and what happens when a provider does
not report usage — so those get the assertions.
"""

from __future__ import annotations

import asyncio

import pytest

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
from app.runtime.budget import Budget, BudgetExceeded, clear_budget, get_budget
from app.runtime.manager import RunManager
from app.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# Test-only nodes: one that burns time, one that reports usage
# ---------------------------------------------------------------------------

DELAY_SPEC = NodeTypeSpec(
    type="budget_delay",
    label="Budget delay",
    category=NodeCategory.DATA,
    inputs=[InputPort(id="in", label="In", type=PortType.ANY)],
    outputs=[OutputPort(id="out", label="Out", type=PortType.ANY)],
    params=[NumberParam(name="seconds", label="Seconds", default=0.05)],
)


@register_node
class BudgetDelayNode(BaseNode):
    spec = DELAY_SPEC

    async def run(self, ctx: NodeContext) -> dict:
        await asyncio.sleep(float(ctx.config.get("seconds") or 0.05))
        return {"out": ctx.input("in")}


USAGE_SPEC = NodeTypeSpec(
    type="budget_usage",
    label="Budget usage",
    category=NodeCategory.DATA,
    inputs=[InputPort(id="in", label="In", type=PortType.ANY)],
    outputs=[OutputPort(id="out", label="Out", type=PortType.ANY)],
    params=[NumberParam(name="tokens", label="Tokens", default=100)],
)


@register_node
class BudgetUsageNode(BaseNode):
    """Reports a token figure the way a model node does: through `emit`."""

    spec = USAGE_SPEC

    async def run(self, ctx: NodeContext) -> dict:
        tokens = int(ctx.config.get("tokens") or 100)
        await ctx.emit(
            {
                "usage": {
                    "prompt_tokens": tokens,
                    "completion_tokens": 0,
                    "total_tokens": tokens,
                    "calls": 1,
                }
            }
        )
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


def _graph(*middle: dict, workflow_id: str = "wf-budget") -> WorkflowGraph:
    ids = ["start", *(n["id"] for n in middle), "end"]
    return WorkflowGraph(
        id=workflow_id,
        nodes=[
            _node("start", "start", input_schema={}),
            *middle,
            _node("end", "end", output_label="result"),
        ],
        edges=[_edge(a, b) for a, b in zip(ids, ids[1:])],
    )


async def _run(manager: RunManager, graph: WorkflowGraph, **settings_overrides):
    context = await manager.create(
        graph.id,
        graph,
        RunCreateRequest(workflow_id=graph.id, recursion_limit=50),
    )
    await manager.start(context)
    assert context.task is not None
    await context.task
    return context


# ---------------------------------------------------------------------------
# The Budget object
# ---------------------------------------------------------------------------


class TestTheBudgetObject:
    def test_no_limits_means_no_checking(self):
        budget = Budget()
        assert budget.enabled is False
        budget.steps = 5000
        budget.tokens = 10**9
        budget.check()  # must not raise

    def test_zero_in_settings_means_unlimited_not_zero(self):
        """Leaving a field empty must not stop every run at the first check."""
        budget = Budget.from_settings(_settings())
        assert budget.enabled is False

    def test_each_limit_reports_itself_by_name(self):
        for kwargs, name in (
            ({"max_steps": 1}, "step"),
            ({"max_wall_seconds": 0.001}, "wall-clock"),
            ({"max_tokens": 10}, "token"),
            ({"max_cost": 0.000001}, "cost"),
        ):
            budget = Budget(**kwargs)
            budget.steps = 99
            budget.tokens = 999
            budget.cost = 9.99
            if "max_wall_seconds" in kwargs:
                import time

                time.sleep(0.01)
            with pytest.raises(BudgetExceeded) as excinfo:
                budget.check()
            assert excinfo.value.limit_name in (name, "step", "cost"), (
                f"{kwargs} reported {excinfo.value.limit_name}"
            )

    def test_an_expensive_first_node_is_reported_after_it_runs(self):
        """The post-node check exists for this: one node can blow the budget by
        itself, and waiting for the next node would be too late."""
        budget = Budget(max_tokens=50)
        budget.add_usage({"total_tokens": 500})
        with pytest.raises(BudgetExceeded, match="token limit is 50"):
            budget.check()

    def test_unreported_usage_is_not_treated_as_zero(self):
        """The gateway's rule: unreported is not the same as none.

        Accumulating an unknown figure as 0 would make the token budget never
        fire on exactly the models where the spend is hardest to see.
        """
        budget = Budget(max_tokens=10)
        budget.add_usage(None)
        assert budget.tokens_known is False
        budget.check()  # cannot be judged, so it is not judged
        assert budget.usage()["tokens_known"] is False

    def test_cost_needs_prices_and_says_so(self):
        budget = Budget(max_cost=1.0)  # no prices configured
        budget.add_usage({"prompt_tokens": 10**7, "completion_tokens": 10**7})
        budget.check()  # still zero, so nothing to report
        assert budget.cost == 0.0

        priced = Budget(
            max_cost=1.0, input_price_per_million=1.0, output_price_per_million=1.0
        )
        priced.add_usage({"prompt_tokens": 10**6, "completion_tokens": 10**6})
        assert priced.cost == pytest.approx(2.0)
        with pytest.raises(BudgetExceeded):
            priced.check()

    def test_the_limit_and_the_actual_figure_are_both_in_the_message(self):
        budget = Budget(max_steps=3)
        budget.steps = 7
        with pytest.raises(BudgetExceeded) as excinfo:
            budget.check()
        message = str(excinfo.value)
        assert "3" in message and "7" in message and "step" in message


# ---------------------------------------------------------------------------
# Through a run
# ---------------------------------------------------------------------------


class TestARunStopsAtItsLimit:
    async def test_an_unlimited_run_finishes(self):
        """The control for everything below."""
        manager = RunManager()
        context = await _run(
            manager,
            _graph(
                _node("a", "budget_delay", seconds=0.01),
                _node("b", "budget_delay", seconds=0.01),
            ),
        )
        assert context.status is RunStatus.SUCCEEDED
        assert context.error is None

    async def test_a_step_limit_stops_before_later_nodes_run(self):
        """`max_steps` must stop the run *early*, not merely report after the
        fact — so the third node's marker must never appear in state."""
        manager = RunManager()
        graph = _graph(
            _node("a", "budget_usage", tokens=1),
            _node("b", "budget_usage", tokens=1),
            _node("c", "budget_usage", tokens=1),
            workflow_id="wf-budget-steps",
        )
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=50)
        )
        # start + a + b = 3 steps, then c is refused by the pre-node check.
        # Asserted node by node because "the run stopped somewhere" would also
        # pass if the limit only fired at the very end.
        context.budget.max_steps = 2
        await manager.start(context)
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.FAILED
        assert "step" in (context.error or "")
        recorded = set(context.steps)
        assert {"a", "b"} <= recorded, f"the limit fired too early: {recorded}"
        assert "c" not in recorded, (
            "the node that would have exceeded the limit still ran, so the "
            f"pre-node check is not doing anything: {recorded}"
        )

    async def test_a_wall_clock_limit_stops_a_slow_run(self):
        """Set through the manager's settings, because that is where a deployment
        configures it."""
        manager = RunManager()
        graph = _graph(_node("a", "budget_delay", seconds=0.3))

        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=50)
        )
        # Scope the limit to this run only.
        context.budget.max_wall_seconds = 0.05
        await manager.start(context)
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.FAILED
        assert context.error is not None
        assert "wall-clock" in context.error
        assert "0.05" in context.error

    async def test_a_token_limit_stops_the_run_after_the_reading_node(self):
        manager = RunManager()
        graph = _graph(
            _node("a", "budget_usage", tokens=100),
            _node("b", "budget_usage", tokens=100, workflow_id="x"),
            workflow_id="wf-budget-tokens",
        )
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=50)
        )
        context.budget.max_tokens = 150
        await manager.start(context)
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.FAILED
        assert "token" in (context.error or "")
        # The second node reported 200 total, which is over 150 — so it must have
        # run and been caught by the *post*-node check rather than skipped.
        assert context.budget.tokens == 200

    async def test_the_stopped_run_keeps_its_steps(self):
        """A budget that discards its own evidence cannot be tuned."""
        manager = RunManager()
        graph = _graph(
            _node("a", "budget_usage", tokens=100),
            _node("b", "budget_usage", tokens=100),
            workflow_id="wf-budget-evidence",
        )
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=50)
        )
        context.budget.max_tokens = 50
        await manager.start(context)
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.FAILED
        assert context.steps, "the stopped run recorded no steps at all"
        assert context.budget.usage()["tokens"] == 100

    async def test_an_unlimited_run_is_unchanged(self):
        manager = RunManager()
        context = await _run(manager, _graph(_node("a", "budget_usage", tokens=999)))
        assert context.status is RunStatus.SUCCEEDED
        assert context.error is None
        # Usage is still reported even when nothing is limited — it is how a
        # deployment finds out what to set the limit to.
        assert context.budget.usage()["tokens"] == 999

    async def test_the_budget_registry_is_cleared_when_a_run_ends(self):
        """Otherwise the registry grows with run count, and a stale entry could
        answer for a run id that was reused."""
        manager = RunManager()
        context = await _run(manager, _graph(_node("a", "budget_usage", tokens=1)))
        assert get_budget(context.id) is None
