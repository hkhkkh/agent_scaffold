"""Task 18 — the per-node execution policy.

Two halves, and the split is deliberate:

1. :class:`TestPolicy` and :class:`TestExecution` drive ``run_with_policy``
   directly. These are fast and hermetic, and they are where the semantics live.
2. :class:`TestCompilerIntegration` goes through the real compiler and a real
   :class:`~app.runtime.manager.RunManager`. That second half is the one that
   matters most, and for the reason Phase 0 wrote down: a control that is correct
   and **not installed** passes every unit test and changes nothing. A policy
   nobody calls is exactly that failure mode, so these tests assert on a node
   actually running under it.

The fake node is installed by patching ``create_node_instance`` in the compiler's
namespace rather than by registering a new node type. Registering one would leak
into the global registry, and other tests assert on the exact set of twelve types.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from langgraph.errors import GraphBubbleUp, GraphInterrupt

from app.graph.schema import RunCreateRequest, RunEventType, RunStatus, WorkflowGraph
from app.runtime import attempts as attempts_module
from app.runtime.attempts import (
    MAX_RETRIES_CEILING,
    AttemptPolicy,
    NodeTimeoutError,
    run_with_policy,
)
from app.runtime.manager import RunManager
from app.settings import Settings


def _settings(**overrides: Any) -> Settings:
    """Deterministic settings: never read the developer's `.env`."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# 1. Resolving the policy
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_defaults_follow_the_deployment_settings(self):
        settings = _settings(node_timeout_seconds=30.0, node_max_retries=2)
        policy = AttemptPolicy.resolve({}, settings)
        assert policy.timeout_seconds == 30.0
        assert policy.max_retries == 2
        assert policy.attempts_allowed == 3

    def test_a_node_config_overrides_the_deployment(self):
        """The node's own config wins, which is what makes per-node tuning work."""
        settings = _settings(node_timeout_seconds=30.0, node_max_retries=0)
        policy = AttemptPolicy.resolve(
            {"node_timeout_seconds": 2.5, "node_max_retries": 1}, settings
        )
        assert policy.timeout_seconds == 2.5
        assert policy.max_retries == 1

    def test_a_zero_budget_means_no_limit(self):
        """`None`, not `0` — a zero-second deadline would fail every node."""
        policy = AttemptPolicy.resolve({"node_timeout_seconds": 0}, _settings())
        assert policy.timeout_seconds is None
        assert policy.deadline_seconds is None

    def test_a_negative_budget_also_means_no_limit(self):
        policy = AttemptPolicy.resolve({"node_timeout_seconds": -5}, _settings())
        assert policy.timeout_seconds is None

    def test_the_retry_ceiling_is_enforced_and_warned_about(self, caplog):
        """A stray extra digit must not turn one failure into a long, paid run."""
        with caplog.at_level(logging.WARNING):
            policy = AttemptPolicy.resolve({"node_max_retries": 999}, _settings())
        assert policy.max_retries == MAX_RETRIES_CEILING
        assert any("ceiling" in r.message for r in caplog.records)

    def test_a_negative_retry_count_becomes_zero(self, caplog):
        with caplog.at_level(logging.WARNING):
            policy = AttemptPolicy.resolve({"node_max_retries": -3}, _settings())
        assert policy.max_retries == 0
        assert any("negative" in r.message for r in caplog.records)

    def test_a_non_numeric_budget_falls_back_rather_than_killing_the_run(
        self, caplog
    ):
        """A typo in the Inspector should not make the whole workflow unrunnable."""
        settings = _settings(node_timeout_seconds=12.0)
        with caplog.at_level(logging.WARNING):
            policy = AttemptPolicy.resolve({"node_timeout_seconds": "soon"}, settings)
        assert policy.timeout_seconds == 12.0

    def test_a_non_numeric_retry_count_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            policy = AttemptPolicy.resolve({"node_max_retries": "yes"}, _settings())
        assert policy.max_retries == 0

    def test_an_empty_string_is_treated_as_absent(self):
        """The Inspector sends `""` for a cleared field, not `None`."""
        settings = _settings(node_timeout_seconds=45.0, node_max_retries=1)
        policy = AttemptPolicy.resolve(
            {"node_timeout_seconds": "", "node_max_retries": ""}, settings
        )
        assert policy.timeout_seconds == 45.0
        assert policy.max_retries == 1

    def test_the_deadline_includes_the_grace_period(self):
        policy = AttemptPolicy(timeout_seconds=10.0, grace_seconds=5.0)
        assert policy.deadline_seconds == 15.0
        assert policy.timeout_seconds == 10.0, "the budget itself is unchanged"


# ---------------------------------------------------------------------------
# 2. Executing under the policy
# ---------------------------------------------------------------------------


class TestExecution:
    async def test_a_node_inside_its_budget_is_untouched(self):
        async def quick():
            await asyncio.sleep(0)
            return "done"

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0)
        assert await run_with_policy(quick, policy=policy) == "done"

    async def test_a_node_that_overruns_is_cut_short(self):
        """The whole point: a hung node must not hold an execution slot forever."""
        async def forever():
            await asyncio.sleep(999)

        policy = AttemptPolicy(timeout_seconds=0.05, grace_seconds=0.05)
        with pytest.raises(NodeTimeoutError) as excinfo:
            await run_with_policy(forever, policy=policy)
        assert "time budget" in str(excinfo.value)

    async def test_the_reported_budget_is_the_authors_number_not_the_deadline(self):
        """Reporting `budget + grace` would name a number the author never typed."""
        async def forever():
            await asyncio.sleep(999)

        policy = AttemptPolicy(timeout_seconds=0.05, grace_seconds=0.05)
        with pytest.raises(NodeTimeoutError) as excinfo:
            await run_with_policy(forever, policy=policy, label="Fetch docs")
        message = str(excinfo.value)
        assert "'Fetch docs'" in message
        assert "0.05s" in message

    async def test_no_budget_means_it_runs_to_completion(self):
        async def slow_but_finite():
            await asyncio.sleep(0.05)
            return 1

        policy = AttemptPolicy(timeout_seconds=None)
        assert await run_with_policy(slow_but_finite, policy=policy) == 1

    async def test_a_failing_node_runs_once_when_retries_are_off(self):
        calls = []

        async def always_fails():
            calls.append(1)
            raise ValueError("nope")

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=0)
        with pytest.raises(ValueError):
            await run_with_policy(always_fails, policy=policy)
        assert len(calls) == 1, "retries are off by default; the node must not repeat"

    async def test_retries_accumulate_to_the_configured_total(self):
        calls = []

        async def always_fails():
            calls.append(1)
            raise ValueError("nope")

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=2)
        with pytest.raises(ValueError):
            await run_with_policy(always_fails, policy=policy)
        assert len(calls) == 3, "one first attempt plus two retries"

    async def test_a_retry_that_succeeds_returns_the_value(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("transient")
            return "recovered"

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=3)
        assert await run_with_policy(flaky, policy=policy) == "recovered"
        assert len(calls) == 3

    async def test_the_retry_hook_sees_the_attempt_number_and_the_error(self):
        seen: list[tuple[int, str]] = []

        async def always_fails():
            raise ValueError("boom")

        async def on_retry(attempt: int, exc: BaseException) -> None:
            seen.append((attempt, type(exc).__name__))

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=2)
        with pytest.raises(ValueError):
            await run_with_policy(always_fails, policy=policy, on_retry=on_retry)
        assert seen == [(1, "ValueError"), (2, "ValueError")], (
            "the hook must fire before each retry, numbered from 1"
        )

    async def test_each_attempt_gets_a_fresh_budget(self):
        """A retry after a timeout must not inherit the dead attempt's clock."""
        elapsed: list[float] = []

        async def times_out_then_answers():
            started = asyncio.get_running_loop().time()
            if not elapsed:
                elapsed.append(started)
                await asyncio.sleep(999)  # will be cut at ~0.1s
            return "second attempt"

        policy = AttemptPolicy(timeout_seconds=0.05, grace_seconds=0.05, max_retries=1)
        assert await run_with_policy(times_out_then_answers, policy=policy) == (
            "second attempt"
        )

    async def test_a_timeout_is_retryable(self):
        """The common cause of a timeout is a hung endpoint — a retry fixes that."""
        calls = []

        async def times_out_once_then_answers():
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(999)
            return "ok"

        policy = AttemptPolicy(timeout_seconds=0.05, grace_seconds=0.05, max_retries=1)
        assert await run_with_policy(times_out_once_then_answers, policy=policy) == "ok"
        assert len(calls) == 2


class TestWhatMustNeverBeRetried:
    async def test_a_bubble_up_control_exception_is_not_retried(self):
        """`interrupt()` pauses a run. Retrying it asks the reviewer twice."""
        calls = []

        async def pauses():
            calls.append(1)
            raise GraphInterrupt("waiting for a human")

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=3)
        with pytest.raises(GraphBubbleUp):
            await run_with_policy(pauses, policy=policy, never_retry=(GraphBubbleUp,))
        assert len(calls) == 1

    async def test_proof_the_guard_is_load_bearing(self):
        """Without `never_retry` the same node *would* repeat.

        This is the falsifiability check for the test above: if the retry loop
        could not repeat a `GraphBubbleUp` under any configuration, the previous
        test would pass for a reason that has nothing to do with the guard.
        """
        calls = []

        async def pauses():
            calls.append(1)
            raise GraphInterrupt("waiting for a human")

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=3)
        with pytest.raises(GraphBubbleUp):
            await run_with_policy(pauses, policy=policy)
        assert len(calls) == 4, "unguarded, the pause is retried like any failure"

    async def test_cancelling_a_retrying_run_stops_it_at_once(self):
        """`CancelledError` is a `BaseException`, so the retry branch cannot see it.

        Relied upon rather than incidental — see the module docstring. Widening
        the `except` to `BaseException` would turn an operator's cancel into
        "retry the thing they just cancelled".
        """
        calls = []

        async def always_fails():
            calls.append(1)
            await asyncio.sleep(0.01)
            raise ValueError("nope")

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=50)
        task = asyncio.create_task(run_with_policy(always_fails, policy=policy))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) < 10, f"cancel was ignored; the node kept retrying ({calls})"


class TestTheRaceWithANodesOwnDeadline:
    async def test_a_nodes_own_timeout_is_not_reattributed(self):
        """A socket that gave up must report that, not "the budget expired".

        Both are `TimeoutError` at the language level (they have been the same
        class since Python 3.11), so the only way to tell them apart is to ask
        the context manager whether *it* triggered.
        """
        async def its_own_deadline():
            await asyncio.sleep(0)
            raise TimeoutError("connection to api.example.com timed out after 3s")

        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=0)
        with pytest.raises(TimeoutError) as excinfo:
            await run_with_policy(its_own_deadline, policy=policy)
        assert not isinstance(excinfo.value, NodeTimeoutError)
        assert "api.example.com" in str(excinfo.value)

    async def test_an_overrun_is_a_node_timeout_error_not_a_builtin_one(self):
        """`TimeoutError` is an `OSError`; the runtime catches `OSError` elsewhere.

        If this class were a subclass, a per-node budget overrun would be picked
        up by error handling written for file descriptors.
        """

        async def forever():
            await asyncio.sleep(999)

        policy = AttemptPolicy(timeout_seconds=0.05, grace_seconds=0.05)
        with pytest.raises(NodeTimeoutError) as excinfo:
            await run_with_policy(forever, policy=policy)
        assert not isinstance(excinfo.value, OSError)


# ---------------------------------------------------------------------------
# 3. Through the compiler and the real runtime
# ---------------------------------------------------------------------------

WF_TESTS = "wf-attempts"


def _node(node_id, node_type, **kw):
    payload = {"id": node_id, "type": node_type, "label": node_id}
    payload.update(kw)
    return payload


def _edge(source, target, **kw):
    payload = {
        "id": f"{source}-{target}",
        "source": source,
        "sourceHandle": None,
        "target": target,
        "targetHandle": "in",
    }
    payload.update(kw)
    return payload


def _wrap_node_factory(monkeypatch, hook):
    """Wrap ``create_node_instance`` so ``hook(inner, ctx)`` can observe or replace.

    Patching the compiler's own reference is the narrowest way in: it is the
    single place node instances are built, so this reaches every node type
    without registering anything in the global (and globally asserted-on)
    registry.
    """
    import app.graph.compiler as compiler_module

    real_factory = compiler_module.create_node_instance

    class _Wrapper:
        def __init__(self, inner):
            self._inner = inner

        async def run(self, ctx):
            return await hook(self._inner, ctx)

    monkeypatch.setattr(
        "app.graph.compiler.create_node_instance",
        lambda node: _Wrapper(real_factory(node)),
    )


def _install_fake_node(monkeypatch, behavior, node_id: str = "p"):
    """Run ``behavior`` for one node, and the real code for every other node.

    Wrapping rather than replacing matters: the Start and End nodes do real work
    (End is what records ``outputs["result"]``), and a blanket fake would make
    every assertion about a completed run meaningless.
    """

    async def hook(inner, ctx):
        if ctx.node.id == node_id:
            return await behavior(ctx)
        return await inner.run(ctx)

    _wrap_node_factory(monkeypatch, hook)


def _graph(config_extra: dict[str, Any] | None = None) -> WorkflowGraph:
    extra = config_extra or {}
    return WorkflowGraph(
        id=WF_TESTS,
        nodes=[
            _node("s", "start", config={"input_schema": {}}),
            # A `prompt` node is only a vehicle for the merged config here; the
            # fake instance ignores the template entirely.
            _node("p", "prompt", config={"template": "x", **extra}),
            _node("e", "end", config={"output_label": "result"}),
        ],
        edges=[_edge("s", "p"), _edge("p", "e")],
    )


async def _execute(graph: WorkflowGraph) -> tuple[RunManager, Any, list[Any]]:
    """Run a graph to a terminal state. Returns the manager, context, and events.

    The manager comes back because :meth:`RunManager.resume` needs the same one —
    the in-memory run map is per-instance.
    """
    manager = RunManager()
    context = await manager.create(
        graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=200)
    )

    events: list[Any] = []

    async def consume():
        async for event in context.bus.subscribe():
            events.append(event)

    collector = asyncio.create_task(consume())
    await manager.start(context)
    assert context.task is not None
    await context.task
    await collector
    return manager, context, events


@pytest.fixture
def tight_grace(monkeypatch):
    """Shrink the grace period so timeout tests finish in milliseconds.

    The production grace exists so a node's own, more specific deadline reports
    first; a test has no node-level deadline to protect.
    """
    monkeypatch.setattr(attempts_module, "GRACE_SECONDS", 0.05)


class TestCompilerIntegration:
    async def test_a_slow_node_fails_inside_its_budget_instead_of_hanging(
        self, monkeypatch, tight_grace
    ):
        async def forever(_ctx):
            await asyncio.sleep(999)

        _install_fake_node(monkeypatch, forever)

        # A short budget, bounded far below the 999s the node asks for. Before
        # task 18 this call would never return.
        _, context, _ = await asyncio.wait_for(
            _execute(_graph({"node_timeout_seconds": 0.1})), timeout=20
        )

        assert context.status is RunStatus.FAILED
        assert "time budget" in (context.error or "")

    async def test_the_default_budget_does_not_touch_a_normal_node(self, monkeypatch):
        """`node_timeout_seconds` defaults to 120s; a fast node must be unaffected."""

        async def quick(_ctx):
            return {"out": "hello"}

        _install_fake_node(monkeypatch, quick)
        _, context, _ = await _execute(_graph())
        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == "hello"

    async def test_the_retry_count_from_the_node_config_is_honoured(self, monkeypatch):
        calls = []

        async def flaky(_ctx):
            calls.append(1)
            if len(calls) < 3:
                raise ValueError("transient")
            return {"out": "ok"}

        _install_fake_node(monkeypatch, flaky)
        _, context, _ = await _execute(_graph({"node_max_retries": 2}))

        assert context.status is RunStatus.SUCCEEDED
        assert len(calls) == 3

    async def test_a_node_runs_exactly_once_when_retries_are_unset(self, monkeypatch):
        """The default. A side effect must not repeat because a node failed."""
        calls = []

        async def always_fails(_ctx):
            calls.append(1)
            raise ValueError("nope")

        _install_fake_node(monkeypatch, always_fails)
        _, context, _ = await _execute(_graph())

        assert context.status is RunStatus.FAILED
        assert len(calls) == 1

    async def test_each_retry_is_visible_on_the_timeline(self, monkeypatch):
        """Otherwise a retried node looks like one slow node."""

        async def always_fails(_ctx):
            raise ValueError("nope")

        _install_fake_node(monkeypatch, always_fails)
        _, _, events = await _execute(_graph({"node_max_retries": 1}))

        messages = [
            e.data.get("message", "")
            for e in events
            if e.type is RunEventType.LOG and e.node_id == "p"
        ]
        assert any("Attempt 1 of 2" in m for m in messages), messages

    async def test_a_node_failure_is_still_wrapped_with_its_identity(self, monkeypatch):
        """The wrapper that names the culprit must survive the policy layer."""

        async def always_fails(_ctx):
            raise ValueError("nope")

        _install_fake_node(monkeypatch, always_fails)
        _, context, _ = await _execute(_graph())
        assert "p" in (context.error or "")
        assert "ValueError: nope" in (context.error or "")


class TestHumanInTheLoopIsNotBrokenByRetries:
    async def test_a_human_node_with_retries_enabled_still_pauses_exactly_once(
        self, monkeypatch
    ):
        """The headline regression risk of task 18.

        A Human node pauses by raising `GraphInterrupt`. If the retry loop treated
        that as a failure, the node would be entered again, raise again, and burn
        every attempt — asking the reviewer the same question repeatedly.

        **What this counts, and why.** The node's own entries — not
        `INTERRUPT_RAISED` events. The manager emits one such event per `ainvoke`
        however many interrupts were raised underneath, so counting events cannot
        see the retries at all. Measured with a throwaway probe: with the guard
        removed this node is entered **4** times and raises 4 `GraphInterrupt`s,
        while the event count stays at 1. The first version of this test asserted
        on the event count and passed against the mutation — a decorative test,
        caught by `_guardprobe_capability.py`.
        """
        entries: list[int] = []

        async def hook(inner, ctx):
            if ctx.node.id == "h":
                entries.append(1)
            return await inner.run(ctx)

        _wrap_node_factory(monkeypatch, hook)

        graph = WorkflowGraph(
            id="wf-hitl-retry",
            nodes=[
                _node("s", "start", config={"input_schema": {}}),
                _node(
                    "h",
                    "human_input",
                    config={
                        "message": "Approve?",
                        "fields": {"value": {"type": "string"}},
                        "node_max_retries": 3,
                    },
                ),
                _node("e", "end", config={"output_label": "result"}),
            ],
            edges=[_edge("s", "h"), _edge("h", "e")],
        )

        manager, context, events = await _execute(graph)

        assert context.status is RunStatus.INTERRUPTED
        assert len(entries) == 1, f"the pause was raised {len(entries)} times"

        # A second, user-visible consequence of the same bug: each retry writes a
        # warning line, so a mishandled pause also spams the timeline.
        warnings = [
            e.data.get("message", "")
            for e in events
            if e.type is RunEventType.LOG and "Attempt" in e.data.get("message", "")
        ]
        assert warnings == [], warnings

        # And it is still resumable, which is the property that actually matters.
        await manager.resume(context.id, {"approved": True, "values": {"value": "ok"}})
        assert context.task is not None
        await asyncio.wait_for(context.task, timeout=20)
        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == {"value": "ok"}

    async def test_proof_a_human_node_would_repeat_without_the_guard(self):
        """Falsifiability: show the retry loop *can* repeat a pause.

        Without this, the test above would still pass if `GraphInterrupt` simply
        dissolved before the retry loop ever saw it. This shows it does not: with
        `never_retry` omitted, the same node raises four times.
        """
        calls = []

        async def pauses():
            calls.append(1)
            raise GraphInterrupt("waiting for a human")

        # No `never_retry` here, exactly as if the compiler forgot to pass it.
        policy = AttemptPolicy(timeout_seconds=5.0, grace_seconds=5.0, max_retries=3)
        with pytest.raises(GraphBubbleUp):
            await run_with_policy(pauses, policy=policy)
        assert len(calls) == 4
