"""Durable checkpoints: a paused run's state outlives the process.

What this file proves, and — just as importantly — what it does not
-------------------------------------------------------------------
LangGraph's `AsyncSqliteSaver` makes a paused run's **state** durable. This file
proves that, end to end, with a real compiled workflow: run to an interrupt, drop
every object, open a *different* saver on the same file, read the state back, and
finish the run.

It does **not** prove that `POST /runs/{id}/resume` works after a restart, and it
cannot: this file exercises savers directly, not HTTP. That claim is only
answerable by rebuilding a run in a new process, and it now *is* answerable —
see ``tests/test_resume_after_restart.py``. What remains here is the warning that
durable state is not the same thing as a working endpoint, because for one task
in this project's history the state was durable, the row still said `interrupted`,
and Resume answered "unknown run".

The distinction is worth the extra class. An earlier round of this project shipped
a control that was correct, tested, and not installed; the lesson recorded was to
be explicit about what a change does *not* cover.

The other half of the file is eviction. Dropping the reference to a per-run saver
frees its memory; dropping a reference to a *shared* saver frees nothing, so the
rows have to be deleted. Without that, this task trades an in-memory leak for a
disk leak — and a durable store grows faster than a memory one because it never
resets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.graph.schema import RunCreateRequest, RunStatus, WorkflowGraph
from app.runtime.checkpoints import (
    CHECKPOINT_MEMORY,
    CHECKPOINT_SQLITE,
    DEFAULT_CHECKPOINT_STORE,
    CheckpointerConfigError,
    CheckpointerProvider,
    MemoryCheckpointerProvider,
    SqliteCheckpointerProvider,
    build_checkpointer_provider,
    get_checkpointer_provider,
    set_checkpointer_provider,
)
from app.runtime.manager import RunManager
from app.settings import Settings


def _settings(**overrides: Any) -> Settings:
    """Deterministic settings: never read the developer's `.env`."""
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def _restore_provider():
    """The provider is process-wide, so every test here must put it back.

    A provider left installed leaks into every later test in the same process,
    which is how a suite starts passing for the wrong reason.
    """
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


def _pausable(workflow_id: str = "wf-ck") -> WorkflowGraph:
    """``start`` -> Human -> ``end``. Pauses, and finishes once answered."""
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
            _node("end", "end", output_label="result"),
        ],
        edges=[_edge("start", "h"), _edge("h", "end")],
    )


def _linear(workflow_id: str = "wf-ck-linear", count: int = 1) -> WorkflowGraph:
    """``start`` -> prompt nodes -> ``end``. Runs straight through."""
    ids = ["start", *(f"p{i}" for i in range(count)), "end"]
    nodes = [_node("start", "start", input_schema={})]
    nodes += [_node(f"p{i}", "prompt", template=f"step {i}") for i in range(count)]
    nodes.append(_node("end", "end", output_label="result"))
    return WorkflowGraph(
        id=workflow_id,
        nodes=nodes,
        edges=[_edge(a, b) for a, b in zip(ids, ids[1:])],
    )


async def _pause(graph: WorkflowGraph, manager: RunManager | None = None):
    """Run a graph until it pauses for a human. Returns the manager and context."""
    manager = manager or RunManager()
    context = await manager.create(
        graph.id, graph, RunCreateRequest(workflow_id=graph.id, recursion_limit=200)
    )
    await manager.start(context)
    assert context.task is not None
    await context.task
    return manager, context


# ---------------------------------------------------------------------------
# 1. Which provider gets built
# ---------------------------------------------------------------------------


class TestSelection:
    def test_the_default_is_durable(self):
        """Durable by default, because the alternative silently loses paused runs.

        A run stopped for a human is only resumable while its checkpoints exist.
        Under a per-run in-memory saver they die with the process, so "pause, go
        home, resume tomorrow" quietly did not work — the run row still said
        interrupted and Resume answered "unknown run".

        This assertion used to say the opposite. It is the thing that recorded the
        decision, so it had to move with it.
        """
        assert build_checkpointer_provider(_settings()).name == CHECKPOINT_SQLITE

    def test_memory_is_opt_in_and_still_per_run(self):
        """The old behaviour, still available, and still per-run.

        Asserted separately from the default so that a future change to the
        default cannot silently make memory shared — which would let two runs
        overwrite each other's checkpoints under the same thread id.
        """
        provider = MemoryCheckpointerProvider()
        assert provider.make() is not provider.make()

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("memory", "memory"),
            ("sqlite", "sqlite"),
            ("  MEMORY  ", "memory"),
            ("", "sqlite"),
        ],
    )
    def test_a_configured_name_selects_that_provider(self, configured, expected, tmp_path):
        settings = _settings(checkpoint_store=configured, data_dir=tmp_path)
        assert build_checkpointer_provider(settings).name == expected

    def test_an_unknown_name_is_an_error_not_a_fallback(self):
        """A typo for `sqlite` must not yield a system that quietly loses paused runs.

        The dangerous direction of a silent fallback: the operator believes
        checkpoints are durable, and finds out only by losing a run they paused.
        """
        with pytest.raises(CheckpointerConfigError) as excinfo:
            build_checkpointer_provider(_settings(checkpoint_store="sqlite3"))
        message = str(excinfo.value)
        assert "sqlite3" in message
        for name in (CHECKPOINT_MEMORY, CHECKPOINT_SQLITE):
            assert name in message

    def test_the_getter_uses_the_same_selection(self):
        set_checkpointer_provider(None)
        assert get_checkpointer_provider().name == CHECKPOINT_SQLITE

    def test_the_fallback_matches_the_setting_default(self):
        """The empty-string fallback and the Settings default must not drift.

        They are two copies of one fact — this module keeps its own so it stays
        importable without the application's configuration. Two copies of a fact
        is how a system ends up with a default that applies differently depending
        on which code path you enter, which is exactly the bug this pair of
        defaults produced the first time the default changed.
        """
        assert Settings.model_fields["checkpoint_store"].default == DEFAULT_CHECKPOINT_STORE

    def test_an_empty_value_falls_back_to_the_default(self):
        """An unset environment variable arrives as `''`, not as absent."""
        assert build_checkpointer_provider(_settings(checkpoint_store="")).name == (
            DEFAULT_CHECKPOINT_STORE
        )

    def test_the_unknown_name_message_names_the_fallback_correctly(self):
        """The message has to name the real default, or it misleads while helping."""
        with pytest.raises(CheckpointerConfigError) as excinfo:
            build_checkpointer_provider(_settings(checkpoint_store="nonsense"))
        assert DEFAULT_CHECKPOINT_STORE in str(excinfo.value)

    def test_the_sqlite_provider_puts_its_file_under_the_data_dir(self, tmp_path):
        provider = build_checkpointer_provider(
            _settings(checkpoint_store="sqlite", data_dir=tmp_path)
        )
        assert isinstance(provider, SqliteCheckpointerProvider)
        assert provider.path == tmp_path / "checkpoints.db"

    def test_the_sqlite_provider_refuses_to_hand_out_a_saver_before_it_is_opened(
        self, tmp_path
    ):
        """A saver with no connection would accept writes and persist nothing.

        That is the worst possible behaviour here: a paused run would look fine in
        the UI and be unresumable after a restart, which is the exact failure this
        whole module exists to remove. So it raises instead.

        Reachable in practice — a test or script that builds a provider and calls
        `make()` without going through the application lifespan.
        """
        provider = SqliteCheckpointerProvider(tmp_path / "never-opened.db")
        with pytest.raises(CheckpointerConfigError, match="opened"):
            provider.make()

    def test_the_memory_provider_has_nothing_to_open_or_clean(self):
        """So the lifespan, which always opens and closes, stays uniform."""
        provider = MemoryCheckpointerProvider()
        asyncio.run(provider.open())
        asyncio.run(provider.forget("any-thread"))
        asyncio.run(provider.close())


# ---------------------------------------------------------------------------
# 2. The point of the whole task: state survives the process
# ---------------------------------------------------------------------------


class TestAPausedRunSurvivesAReopen:
    async def test_a_paused_run_is_resumable_by_a_different_saver(self, tmp_path):
        """The headline claim, measured rather than assumed.

        Everything from the first phase is dropped before the second begins: the
        manager goes out of scope, and its saver is closed. A different
        `AsyncSqliteSaver` opens the same file and has to find the state, or a
        restart would lose exactly the runs the feature exists to protect.
        """
        db = tmp_path / "checkpoints.db"
        graph = _pausable()

        # ---- first process ----
        first = SqliteCheckpointerProvider(db)
        await first.open()
        set_checkpointer_provider(first)

        manager, context = await _pause(graph)
        assert context.status is RunStatus.INTERRUPTED
        thread_id = context.thread_id

        # Drop everything the first phase held. `del` plus the closed connection
        # is what a restart amounts to for this test.
        del manager, context
        set_checkpointer_provider(None)
        await first.close()

        assert db.exists(), "the first phase wrote no database at all"

        # ---- second process ----
        second = SqliteCheckpointerProvider(db)
        await second.open()
        set_checkpointer_provider(second)
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            from langgraph.types import Command

            from app.graph.compiler import compile_graph

            saver = second.make()
            assert isinstance(saver, AsyncSqliteSaver)

            config = {"configurable": {"thread_id": thread_id}}
            assert await saver.aget_tuple(config) is not None, (
                "the reopened saver cannot see the paused run's state"
            )

            compiled = compile_graph(graph, checkpointer=saver)
            resumed = await compiled.ainvoke(
                Command(resume={"approved": True, "values": {"value": "ok"}}), config=config
            )
            body = getattr(resumed, "value", resumed)
            assert body.get("output") == {"result": {"value": "ok"}}
        finally:
            await second.close()

    async def test_the_memory_provider_loses_it(self, tmp_path):
        """The control. Without this, the test above would pass even if the
        durable path proved nothing — the difference is the whole point."""
        graph = _pausable()
        set_checkpointer_provider(MemoryCheckpointerProvider())
        _, context = await _pause(graph)
        assert context.status is RunStatus.INTERRUPTED

        from langgraph.checkpoint.memory import InMemorySaver

        from app.graph.compiler import compile_graph

        # A brand-new in-memory saver, which is what a restart produces.
        fresh = InMemorySaver()
        config = {"configurable": {"thread_id": context.thread_id}}
        assert await fresh.aget_tuple(config) is None

        # And it stays a usable saver, so the assertion above is about content,
        # not about a broken object.
        compiled = compile_graph(graph, checkpointer=fresh)
        assert compiled is not None


# ---------------------------------------------------------------------------
# 3. Eviction has to delete, or durability becomes a disk leak
# ---------------------------------------------------------------------------


class TestEvictionDeletesTheRows:
    async def test_a_finished_run_leaves_no_checkpoints_behind(self, tmp_path):
        db = tmp_path / "checkpoints.db"
        provider = SqliteCheckpointerProvider(db)
        await provider.open()
        set_checkpointer_provider(provider)
        try:
            graph = _linear(count=2)
            manager = RunManager()
            context = await manager.create(
                graph.id,
                graph,
                RunCreateRequest(workflow_id=graph.id, recursion_limit=200),
            )
            await manager.start(context)
            assert context.task is not None
            await context.task

            assert context.status is RunStatus.SUCCEEDED
            config = {"configurable": {"thread_id": context.thread_id}}
            assert await provider.make().aget_tuple(config) is None, (
                "a terminal run's checkpoints were left on disk. With a durable "
                "saver these never reset, so this is a leak that only grows."
            )
        finally:
            await provider.close()

    async def test_an_interrupted_run_keeps_its_checkpoints(self, tmp_path):
        """The exemption, and the reason durable checkpoints are worth anything.

        A paused run's checkpoints are the only thing `resume` can read. Deleting
        them on eviction would make the feature delete its own payload — and it
        would look like success, because the run does reach a terminal state.
        """
        db = tmp_path / "checkpoints.db"
        provider = SqliteCheckpointerProvider(db)
        await provider.open()
        set_checkpointer_provider(provider)
        try:
            _, context = await _pause(_pausable())
            assert context.status is RunStatus.INTERRUPTED

            config = {"configurable": {"thread_id": context.thread_id}}
            assert await provider.make().aget_tuple(config) is not None, (
                "a run waiting on a human had its resume state deleted"
            )
        finally:
            await provider.close()

    async def test_the_cleanup_mirrors_release_rather_than_re_deciding(self, tmp_path):
        """Both rules are keyed on the same status, so they cannot disagree.

        `release` declines to touch an interrupted run; the cleanup does the same.
        If these drifted, one would free state the other still expects.
        """
        set_checkpointer_provider(MemoryCheckpointerProvider())
        _, paused = await _pause(_pausable())
        assert paused.status is RunStatus.INTERRUPTED
        assert paused.retired_at is None, (
            "an interrupted run was marked droppable, so eviction would free the "
            "state a human is still going to answer for"
        )
        assert paused.checkpointer is not None


# ---------------------------------------------------------------------------
# 4. What this change does not cover
# ---------------------------------------------------------------------------


class TestTheBoundaryThisFileDoesNotCover:
    def test_this_file_still_does_not_prove_the_endpoint_works(self):
        """Documents the boundary. Durable state is necessary, not sufficient.

        Read carefully, because the *shape* of this test is the point. An earlier
        version was named `test_a_restart_alone_does_not_make_resume_work` and
        asserted the same thing — ``RunManager.get`` only looks in memory. That
        assertion is still true, and the conclusion drawn from it is now **false**:
        the endpoint works after a restart, because ``RunManager.resume`` falls
        back to ``hydrate`` when ``get`` comes back empty.

        So this file cannot answer the question by itself, and it never could —
        it exercises savers directly, not HTTP. The real coverage is
        ``tests/test_resume_after_restart.py``, which builds a **new** manager and
        resumes through it. What is worth keeping here is the warning that
        ``get`` being memory-only says nothing about whether resume works: that
        gap is exactly what a reader would misread.
        """
        manager = RunManager()
        assert manager.get("a-run-that-only-exists-on-disk") is None, (
            "if `get` gained disk-backed lookup, this test is asserting the wrong "
            "thing entirely — see tests/test_resume_after_restart.py for the path "
            "that does reach a rebuilt run"
        )
        assert hasattr(manager, "hydrate"), (
            "the rebuild is what closes the gap this test describes; without it "
            "this boundary is real again"
        )
