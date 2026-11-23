"""The concurrency gate: a bound, a visible line, and a way out of it.

Small on purpose. Three behaviours carry the feature — the limit holds, waiting
runs still finish, and a queued run can leave the line — plus the settings wiring,
which is the part that silently does nothing if it is wrong.
"""

from __future__ import annotations

import asyncio

import pytest

from app.graph.schema import (
    InputPort,
    NodeCategory,
    NodeTypeSpec,
    OutputPort,
    NumberParam,
    PortType,
    RunCreateRequest,
    RunStatus,
    WorkflowGraph,
)
from app.nodes.base import BaseNode, NodeContext
from app.nodes.registry import register_node
from app.runtime.manager import RunManager
from app.runtime.queue import (
    InMemoryTaskQueue,
    QueueConfigError,
    RunCancelledWhileQueued,
    build_task_queue,
    set_task_queue,
)
from app.settings import Settings


@pytest.fixture(autouse=True)
def _restore_queue():
    yield
    set_task_queue(None)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _node(node_id: str, node_type: str, **config):
    return {"id": node_id, "type": node_type, "label": node_id, "config": config}


#: A node that takes a known amount of time.
#:
#: Registered here rather than reusing the Code node, because the Code sandbox
#: deliberately refuses `import time` — the first version of this file tried it
#: and every run failed with `Module 'time' is not allowed`. Sleeping from a real
#: node gives deterministic timing, which is what the concurrency assertions need.
DELAY_SPEC = NodeTypeSpec(
    type="test_delay",
    label="Test delay",
    category=NodeCategory.DATA,
    inputs=[InputPort(id="in", label="In", type=PortType.ANY)],
    outputs=[OutputPort(id="out", label="Out", type=PortType.ANY)],
    params=[NumberParam(name="seconds", label="Seconds", default=0.1)],
)


@register_node
class DelayNode(BaseNode):
    spec = DELAY_SPEC

    async def run(self, ctx: NodeContext) -> dict:
        await asyncio.sleep(float(ctx.config.get("seconds") or 0.1))
        return {"out": ctx.input("in")}


def _edge(source: str, target: str):
    return {
        "id": f"{source}->{target}",
        "source": source,
        "sourceHandle": "out",
        "target": target,
        "targetHandle": "in",
    }


def _slow(workflow_id: str = "wf-queue", delay: float = 0.2) -> WorkflowGraph:
    """A one-node graph that takes a known amount of time."""
    return WorkflowGraph(
        id=workflow_id,
        nodes=[
            _node("start", "start", input_schema={}),
            _node("c", "test_delay", seconds=delay),
            _node("end", "end", output_label="result"),
        ],
        edges=[_edge("start", "c"), _edge("c", "end")],
    )


# ---------------------------------------------------------------------------
# The queue itself
# ---------------------------------------------------------------------------


class TestTheQueueItself:
    async def test_a_limit_of_zero_means_unlimited(self):
        queue = InMemoryTaskQueue(0)
        await asyncio.gather(*(queue.acquire(str(i)) for i in range(50)))
        assert len(queue.snapshot()["active"]) == 50

    async def test_the_limit_is_never_exceeded(self):
        queue = InMemoryTaskQueue(2)
        peak = 0

        async def worker(name: str) -> None:
            nonlocal peak
            await queue.acquire(name)
            peak = max(peak, len(queue.snapshot()["active"]))
            await asyncio.sleep(0.01)
            queue.release(name)

        await asyncio.gather(*(worker(str(i)) for i in range(8)))
        assert peak == 2, f"peak concurrency was {peak}, limit was 2"

    async def test_waiters_are_served_in_order(self):
        """Strict FIFO: a burst of short runs must not starve the one at the front."""
        queue = InMemoryTaskQueue(1)
        order: list[str] = []

        async def worker(name: str) -> None:
            await queue.acquire(name)
            order.append(name)
            await asyncio.sleep(0.005)
            queue.release(name)

        tasks = [asyncio.create_task(worker(str(i))) for i in range(5)]
        await asyncio.sleep(0)  # let them all enqueue
        assert queue.snapshot()["waiting"] == ["1", "2", "3", "4"]
        await asyncio.gather(*tasks)
        assert order == ["0", "1", "2", "3", "4"]

    async def test_a_waiting_run_can_leave_the_line(self):
        queue = InMemoryTaskQueue(1)
        await queue.acquire("holder")

        waiter = asyncio.create_task(queue.acquire("leaver"))
        await asyncio.sleep(0)
        assert queue.position("leaver") == 1
        assert queue.drop("leaver") is True

        with pytest.raises(RunCancelledWhileQueued):
            await waiter

    async def test_dropping_a_run_that_is_not_waiting_answers_false(self):
        """A run already executing is not removable here — that is `cancel`."""
        queue = InMemoryTaskQueue(1)
        await queue.acquire("busy")
        assert queue.drop("busy") is False
        assert queue.drop("never-seen") is False

    async def test_releasing_a_slot_wakes_the_next_waiter(self):
        queue = InMemoryTaskQueue(1)
        await queue.acquire("first")
        second = asyncio.create_task(queue.acquire("second"))
        await asyncio.sleep(0)
        assert queue.position("second") == 1

        queue.release("first")
        await asyncio.wait_for(second, timeout=2)
        assert queue.snapshot()["active"] == ["second"]


class TestConfiguration:
    def test_memory_is_the_default(self):
        queue = build_task_queue(_settings())
        assert queue.name == "memory"
        assert queue.limit == 0

    def test_the_limit_comes_from_settings(self):
        assert build_task_queue(_settings(max_concurrent_runs=3)).limit == 3

    def test_an_unknown_queue_is_an_error_not_a_fallback(self):
        """Same rule as RUN_STORE and SANDBOX_RUNNER: a deployment that asked for
        a queue it cannot have must stop, not quietly run unbounded."""
        with pytest.raises(QueueConfigError, match="nonsense"):
            build_task_queue(_settings(task_queue="nonsense"))


# ---------------------------------------------------------------------------
# Through the manager
# ---------------------------------------------------------------------------


class TestRunsGoThroughTheGate:
    async def test_runs_beyond_the_limit_wait_and_still_finish(self):
        """The whole point: nothing is rejected, and the limit holds."""
        set_task_queue(InMemoryTaskQueue(2))
        manager = RunManager()
        graph = _slow(delay=0.15)

        contexts = [
            await manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
            for _ in range(5)
        ]
        for context in contexts:
            await manager.start(context)

        # Right after starting, some must be waiting rather than running.
        await asyncio.sleep(0.05)
        statuses = {c.status for c in contexts}
        assert RunStatus.QUEUED in statuses, (
            f"nothing queued with a limit of 2 and 5 runs: {statuses}"
        )

        await asyncio.gather(*(c.task for c in contexts if c.task))
        assert {c.status for c in contexts} == {RunStatus.SUCCEEDED}, (
            "a queued run did not finish: "
            f"{[(c.id, c.status, c.error) for c in contexts]}"
        )

    async def test_a_queued_run_reports_its_position_then_starts(self):
        set_task_queue(InMemoryTaskQueue(1))
        manager = RunManager()
        graph = _slow(delay=0.12)

        first = await manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
        second = await manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
        await manager.start(first)
        await manager.start(second)

        await asyncio.sleep(0.03)
        assert second.status is RunStatus.QUEUED
        assert first.status is RunStatus.RUNNING

        await asyncio.gather(first.task, second.task)
        assert second.status is RunStatus.SUCCEEDED

    async def test_cancelling_a_queued_run_removes_it_without_running_it(self):
        """'Never entered RUNNING' is the assertion — a run that started and then
        stopped would also end up cancelled, and would have done work."""
        set_task_queue(InMemoryTaskQueue(1))
        manager = RunManager()
        graph = _slow(delay=0.12)

        holder = await manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
        waiter = await manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
        await manager.start(holder)
        await manager.start(waiter)

        await asyncio.sleep(0.03)
        assert waiter.status is RunStatus.QUEUED

        cancelled = await manager.cancel(waiter.id)
        assert cancelled.status is RunStatus.CANCELLED
        assert "before it started" in (cancelled.error or "")
        assert waiter.status is not RunStatus.RUNNING

        await holder.task

    async def test_shutdown_leaves_nothing_waiting(self):
        set_task_queue(InMemoryTaskQueue(1))
        manager = RunManager()
        graph = _slow(delay=0.3)

        contexts = [
            await manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
            for _ in range(3)
        ]
        for context in contexts:
            await manager.start(context)
        await asyncio.sleep(0.03)

        await manager.shutdown()

        stuck = [c.id for c in contexts if c.status is RunStatus.QUEUED]
        assert not stuck, f"runs left queued after shutdown: {stuck}"

    async def test_creating_runs_concurrently_keeps_every_one(self):
        """What `_lock` is for: `create` awaits between compiling and registering,
        so overlapping calls can interleave there."""
        set_task_queue(InMemoryTaskQueue(0))
        manager = RunManager()
        graph = _slow(delay=0.01)

        contexts = await asyncio.gather(
            *(
                manager.create(graph.id, graph, RunCreateRequest(workflow_id=graph.id))
                for _ in range(30)
            )
        )
        assert len({c.id for c in contexts}) == 30
        assert len(manager.retained_ids()) == 30

        await asyncio.gather(*(c.task for c in contexts if c.task))
