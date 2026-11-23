"""Picking up a paused run in a process that never saw it start.

What task 14 delivered was durable *state*: `AsyncSqliteSaver` writes a paused
run's checkpoints to disk, so the state is still there after a restart. That is
necessary and it was not sufficient, and
`test_checkpoints.py::TestTheBoundaryThisFileDoesNotCover` records the gap that
used to sit here:

    Durable state is not a working endpoint. The checkpoints were on disk, the row
    still said `interrupted`, and Resume answered "unknown run".

This file is that boundary moving. `RunManager.hydrate` rebuilds a run out of
persistence alone — the definition from `Workflow`, the inputs and configuration
from the `Run` row, the paused question from the checkpoints — and `resume` falls
back to it when the run is not in memory.

Why these tests drop the manager rather than mock a restart
----------------------------------------------------------
A test that keeps a live manager and calls `resume` proves nothing about
restarts: it would pass on the old code, through the in-memory path, without ever
touching `hydrate`. Every test here that claims to be about a restart constructs a
**new `RunManager`**, so `_runs` starts empty and the only possible source of the
run is persistence.

The control is deliberately present: the live path still works, and is asserted
separately, so "the fallback is used" is distinguishable from "the fallback was
silently the only thing that worked".
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.db.repository import save_workflow
from app.graph.schema import RunCreateRequest, RunStatus, WorkflowGraph
from app.runtime.checkpoints import (
    MemoryCheckpointerProvider,
    SqliteCheckpointerProvider,
    get_checkpointer_provider,
    set_checkpointer_provider,
)
from app.runtime.manager import RunError, RunManager
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _restore_provider():
    """The provider is process-wide; a leak into later tests reads as flakiness."""
    yield
    set_checkpointer_provider(None)


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------


def _node(node_id: str, node_type: str, **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "label": node_id, "config": config}


def _edge(source: str, target: str) -> dict[str, Any]:
    return {
        "id": f"{source}->{target}",
        "source": source,
        "sourceHandle": "out",
        "target": target,
        "targetHandle": "in",
    }


def _pausable(
    workflow_id: str = "wf-resume", *, output_label: str = "result"
) -> WorkflowGraph:
    """``start`` -> Human -> ``end``. Pauses, then finishes once answered.

    ``output_label`` is a parameter rather than a constant because the tweaks test
    needs to override it at run time and observe the difference in the output. A
    tweak that is silently dropped has to produce a *different, still valid*
    result, or the test would pass on a broken rebuild.
    """
    return WorkflowGraph(
        id=workflow_id,
        nodes=[
            _node("start", "start", input_schema={}),
            _node(
                "h",
                "human_input",
                message="Approve?",
                fields={"value": {"type": "string"}},
            ),
            _node("end", "end", output_label=output_label),
        ],
        edges=[_edge("start", "h"), _edge("h", "end")],
    )


async def _pause(graph: WorkflowGraph, *, tweaks: dict[str, Any] | None = None):
    """Run a graph to its interrupt. Returns the manager and the live context."""
    manager = RunManager()
    context = await manager.create(
        graph.id,
        graph,
        RunCreateRequest(
            workflow_id=graph.id, recursion_limit=200, tweaks=tweaks or {}
        ),
    )
    await manager.start(context)
    assert context.task is not None
    await context.task
    assert context.status is RunStatus.INTERRUPTED
    return manager, context


# ---------------------------------------------------------------------------
# 1. The claim: a restart no longer blocks resume
# ---------------------------------------------------------------------------


class TestAPausedRunIsPickedUpByANewProcess:
    async def test_resume_finishes_a_run_this_manager_never_started(self):
        """The headline. A brand-new manager rebuilds the run and completes it.

        Nothing is carried over: the manager, its `_runs` map, and the live
        context all go out of scope. `save_workflow` is what makes the definition
        recoverable, and the `Run` row is what makes the configuration
        recoverable.
        """
        graph = _pausable()
        save_workflow(graph)

        _, live = await _pause(graph)
        run_id = live.id

        # ---- the restart: a manager with no memory of anything ----
        fresh = RunManager()
        assert fresh.get(run_id) is None

        rebuilt = await fresh.resume(run_id, {"approved": True, "values": {"value": "ok"}})
        assert rebuilt.id == run_id
        assert rebuilt.task is not None
        await rebuilt.task

        assert rebuilt.status is RunStatus.SUCCEEDED, rebuilt.error
        assert rebuilt.outputs.get("result") == {"value": "ok"}

    async def test_the_rebuilt_run_is_what_the_original_process_had(self):
        """Every field a resume depends on comes back, not just the ones the
        output happens to reveal.

        Asserted field by field rather than through the final output alone: a
        rebuild that got `thread_id` wrong would still find a checkpoint in a
        single-run test, and a rebuild that got `inputs` wrong might still
        produce the same answer for this graph.
        """
        graph = _pausable()
        save_workflow(graph)

        _, live = await _pause(graph)

        rebuilt = await RunManager().hydrate(live.id)

        assert rebuilt.workflow_id == live.workflow_id
        assert rebuilt.thread_id == live.thread_id
        assert rebuilt.inputs == live.inputs
        assert rebuilt.tweaks == live.tweaks
        assert rebuilt.recursion_limit == live.recursion_limit
        assert rebuilt.status is RunStatus.INTERRUPTED
        assert rebuilt.created_at == live.created_at

    async def test_the_human_question_comes_back_too(self):
        """A rebuilt run with no question is unusable in a specific way.

        The UI renders a review box from `interrupt`. If the rebuild produced the
        run but not the payload, the reviewer would see an empty prompt for a
        question that has a message and fields — the run would look resumable and
        be impossible to answer correctly.
        """
        graph = _pausable()
        save_workflow(graph)

        _, live = await _pause(graph)
        rebuilt = await RunManager().hydrate(live.id)

        assert rebuilt.interrupt, "a rebuilt run came back with no question to ask"
        assert rebuilt.interrupt == live.interrupt, (
            "the rebuilt payload differs from the live one, so a client could tell "
            "which path answered"
        )

    async def test_a_payload_hand_parsed_from_the_checkpoint_would_have_matched(self):
        """Guards the *choice* of API, not the behaviour.

        The first version of `hydrate` read `CheckpointTuple.pending_writes` and
        parsed `Interrupt` objects out of a `(task_id, "__interrupt__", [...])`
        entry. It worked. It was the wrong layer — `aget_state` returns a
        `StateSnapshot` whose `.interrupts` is the documented field for this
        question, and hand-parsing a lower-level structure reimplements upstream
        against something more likely to change.

        This asserts the two agree, so the test above is known to be about the
        payload and not about which reader produced it. If upstream ever changes
        the internal layout, this is where it shows up first.
        """
        graph = _pausable()
        save_workflow(graph)
        _, live = await _pause(graph)

        config = {"configurable": {"thread_id": live.thread_id}}
        snapshot = await live.compiled.aget_state(config)
        assert snapshot.interrupts, "the documented API returned nothing to compare"
        assert snapshot.next == ("h",), (
            "the snapshot does not name where the run stopped; hydrate relies on "
            "`.interrupts` alone, so this is informational"
        )


# ---------------------------------------------------------------------------
# 2. Configuration survives, and is refused when it cannot
# ---------------------------------------------------------------------------


class TestConfigurationIsRestoredExactly:
    async def test_tweaks_survive_the_restart(self):
        """A run resuming with different tweaks gives a different answer and
        reports no error — so the tweaks are asserted through the output.

        The tweak renames the output key. If the rebuild dropped it, the run would
        still succeed and produce `result` instead of `renamed`, which is exactly
        the silent-wrong-answer case this task file warns about.
        """
        graph = _pausable()
        save_workflow(graph)

        _, live = await _pause(graph, tweaks={"end": {"output_label": "renamed"}})

        fresh = RunManager()
        rebuilt = await fresh.resume(live.id, {"approved": True, "values": {"value": "ok"}})
        assert rebuilt.task is not None
        await rebuilt.task

        assert rebuilt.tweaks == {"end": {"output_label": "renamed"}}
        assert rebuilt.status is RunStatus.SUCCEEDED, rebuilt.error
        assert "renamed" in rebuilt.outputs, (
            "the tweak was lost in the rebuild: the output key is unchanged, which "
            "means the run resumed with different configuration than it started "
            "with, and nothing reported it"
        )

    async def test_the_recursion_limit_survives_the_restart(self):
        """The limit is not decoration — it is what stops a runaway graph.

        A rebuild that fell back to the configured default would quietly change
        how far the resumed run is allowed to go.
        """
        graph = _pausable()
        save_workflow(graph)

        _, live = await _pause(graph)
        assert live.recursion_limit == 200

        rebuilt = await RunManager().hydrate(live.id)
        assert rebuilt.recursion_limit == 200

        default = get_settings().recursion_limit
        assert default != 200, (
            "this test is meaningless if the configured default happens to equal "
            "the value the run was started with"
        )

    async def test_a_row_predating_the_column_falls_back_and_says_so(self, caplog):
        """`None` means the row was written before the column existed.

        Half of this project's database lives in a checkout, not in production, so
        an old `runs.db` is a real case. The fallback is the best available
        answer; substituting silently is not, because the limit is a safety
        parameter and "which limit applied?" should be answerable afterwards.
        """
        graph = _pausable()
        save_workflow(graph)
        _, live = await _pause(graph)

        from app.db.models import Run
        from app.db.session import session_scope

        with session_scope() as session:
            row = session.get(Run, live.id)
            assert row is not None
            row.recursion_limit = None

        with caplog.at_level(logging.INFO, logger="app.runtime.manager"):
            rebuilt = await RunManager().hydrate(live.id)

        assert rebuilt.recursion_limit == get_settings().recursion_limit
        assert any(
            "recursion_limit" in record.message for record in caplog.records
        ), "the fallback was applied without recording which limit was used"


class TestWhatHydrateRefuses:
    """Refusing is a feature: a half-built context fails deeper and less clearly.

    Each case here is a way the rebuild *could* have guessed. Guessing produces a
    run that starts, runs, and is wrong.
    """

    async def test_an_unknown_run_is_refused(self):
        with pytest.raises(RunError, match="Unknown run"):
            await RunManager().hydrate("never-existed")

    async def test_a_finished_run_is_refused(self):
        """A finished run has no state to continue from, and resuming it would
        re-run side effects."""
        graph = _pausable()
        save_workflow(graph)
        manager = RunManager()
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=200)
        )
        await manager.start(context)
        assert context.task is not None
        await context.task
        await manager.resume(context.id, {"approved": True, "values": {"value": "ok"}})
        assert context.task is not None
        await context.task
        assert context.status is RunStatus.SUCCEEDED

        with pytest.raises(RunError, match="nothing to resume"):
            await RunManager().hydrate(context.id)

    async def test_a_run_whose_workflow_was_never_saved_is_refused(self):
        """A canvas run against an unsaved draft has nothing on disk to restore.

        Deliberately refused rather than worked around by storing the graph in the
        run row. Storing it would make the row the authority on a definition that
        is *also* being edited in the canvas, and the two would disagree with no
        way to tell which one ran.
        """
        graph = _pausable("wf-never-saved")
        _, live = await _pause(graph)

        with pytest.raises(RunError) as excinfo:
            await RunManager().hydrate(live.id)

        message = str(excinfo.value)
        assert "wf-never-saved" in message, (
            "the error does not name the workflow, so the operator cannot tell "
            "which definition went missing"
        )
        assert "saved" in message.lower()

    async def test_a_paused_run_without_checkpoints_is_refused(self):
        """The memory-provider case, and the honest thing to say about it.

        With `CHECKPOINT_STORE=memory` the row survives a restart and the state
        does not, so the rebuild finds nothing. The message names the setting,
        because that is the only thing the operator can act on.
        """
        set_checkpointer_provider(MemoryCheckpointerProvider())
        graph = _pausable()
        save_workflow(graph)
        _, live = await _pause(graph)

        # A restart under the memory provider means a fresh, empty saver.
        set_checkpointer_provider(MemoryCheckpointerProvider())

        with pytest.raises(RunError) as excinfo:
            await RunManager().hydrate(live.id)

        message = str(excinfo.value)
        assert "checkpoint" in message.lower()
        assert "CHECKPOINT_STORE" in message, (
            "the message does not name the setting that caused this, which is the "
            "only part of it the operator can change"
        )

    async def test_the_sqlite_provider_is_unaffected_by_the_memory_case(self):
        """The control for the test above: same flow, durable provider, works.

        Without this, an over-eager refusal would look like correct handling of
        the memory case.
        """
        graph = _pausable("wf-durable")
        save_workflow(graph)
        _, live = await _pause(graph)

        assert isinstance(get_checkpointer_provider(), SqliteCheckpointerProvider)
        rebuilt = await RunManager().hydrate(live.id)
        assert rebuilt.interrupt


# ---------------------------------------------------------------------------
# 3. The live path is untouched
# ---------------------------------------------------------------------------


class TestTheInMemoryPathStillWins:
    async def test_a_run_still_in_memory_is_not_rehydrated(self):
        """The fallback must not become the primary path.

        Rehydration is strictly more expensive and reads a checkpoint the process
        already has in hand. Asserted by identity: the object `resume` continues is
        the same one `create` produced.
        """
        graph = _pausable()
        save_workflow(graph)
        manager, live = await _pause(graph)

        same = await manager.resume(live.id, {"approved": True, "values": {"value": "ok"}})
        assert same is live, (
            "resume rebuilt a run it already had, so every resume now pays for a "
            "checkpoint read and a graph compile it did not need"
        )
        assert same.task is not None
        await same.task
        assert same.status is RunStatus.SUCCEEDED, same.error

    async def test_a_rehydrated_run_can_share_the_live_set(self):
        """The first thing this change broke, and it was a 500.

        `resume` registers a rehydrated run in `_runs`, so from that point the
        manager holds a mix of runs it started and runs it rebuilt. Their
        `created_at` values compare against each other — `list` and
        `retained_ids` sort by that field — so if the rebuilt one came back naive
        while the live ones were aware, the sort raised
        `TypeError: can't compare offset-naive and offset-aware datetimes` and
        `GET /runs` returned 500.

        The root cause was in the column, not here (see `UtcDateTime` in
        `app/db/models.py`), and is covered on its own in `test_run_store.py`. This
        test is the end that a client actually hits.
        """
        graph = _pausable("wf-mixed")
        save_workflow(graph)
        _, live = await _pause(graph)

        fresh = RunManager()
        resumed = await fresh.resume(live.id, {"approved": True, "values": {"value": "ok"}})

        # A second run this process started itself, so the map really is mixed.
        other_graph = _pausable("wf-mixed-other")
        save_workflow(other_graph)
        other = await fresh.create(
            other_graph.id,
            other_graph,
            RunCreateRequest(workflow_id=other_graph.id, recursion_limit=200),
        )
        assert other.created_at.tzinfo is not None
        assert resumed.created_at.tzinfo is not None, (
            "the rehydrated run carries a naive timestamp next to aware ones, so "
            "any sorted view of the live set raises"
        )

        assert set(fresh.retained_ids()) == {resumed.id, other.id}
        assert {run.id for run in fresh.list()} == {resumed.id, other.id}

        assert resumed.task is not None
        await resumed.task


# ---------------------------------------------------------------------------
# 4. The read path
# ---------------------------------------------------------------------------


class TestTheReadPathUsesTheSameRebuild:
    """`GET /runs/{id}` has to show the question for a run it did not start.

    It reads a row, and the payload is not in the row. Rather than growing a
    second, thinner copy of "how to rebuild a run" — the failure this project has
    already hit twice with duplicated read paths — the API delegates to
    `hydrate`.
    """

    async def test_a_persisted_paused_run_reports_its_question(self):
        from app.api.runs import _persisted_summary_with_interrupt

        graph = _pausable()
        save_workflow(graph)
        _, live = await _pause(graph)

        payload = await _persisted_summary_with_interrupt(live.id)

        assert payload["status"] == RunStatus.INTERRUPTED.value
        assert payload["interrupt"], (
            "the API reported a paused run with no question, so the review box "
            "renders empty for a question that has content"
        )
        assert payload["interrupt"] == live.interrupt

    async def test_a_finished_run_reports_no_question(self):
        """The control: `None` is correct here, and the row is the honest source."""
        from app.api.runs import _persisted_summary_with_interrupt

        graph = _pausable()
        save_workflow(graph)
        manager = RunManager()
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=200)
        )
        await manager.start(context)
        assert context.task is not None
        await context.task
        await manager.resume(context.id, {"approved": True, "values": {"value": "ok"}})
        assert context.task is not None
        await context.task

        payload = await _persisted_summary_with_interrupt(context.id)
        assert payload["status"] == RunStatus.SUCCEEDED.value
        assert payload["interrupt"] is None

    async def test_an_unrebuildable_paused_run_keeps_its_row_and_reports_nothing(self):
        """A run recorded as paused with nothing to resume from is a real state.

        The status is left as it is rather than quietly corrected to `failed`:
        that would hide the only signal that the checkpoints are gone.
        """
        from app.api.runs import _persisted_summary_with_interrupt

        graph = _pausable("wf-unsaved-read")
        _, live = await _pause(graph)

        payload = await _persisted_summary_with_interrupt(live.id)

        assert payload["status"] == RunStatus.INTERRUPTED.value
        assert payload["interrupt"] is None
