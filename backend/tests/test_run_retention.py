"""Run retention: a long-lived backend must not accumulate runs forever.

``RunManager._runs`` was append-only. Every run it had ever executed stayed in
that dict, and one :class:`RunContext` is not small:

* its LangGraph checkpointer stores a **full state snapshot per super-step** and
  lives for the life of the process, so the cost was unbounded in two directions
  at once — runs × super-steps;
* its event bus retains up to ~2200 events, each carrying a node's real input and
  output payload.

The process therefore got heavier with every run, whether or not anything was
still listening, and nothing removed an entry because nothing was supposed to.

The fix has two halves, and these tests are split the same way:

* **release** — free the live-only state as soon as a run reaches a terminal
  state (:meth:`RunContext.release`, called from ``RunManager._retire``);
* **evict** — drop the context entirely, bounded by age *and* count
  (:meth:`RunManager.prune`).

Every test here is written so that it **fails on the old behaviour**. That is not
decorative: an earlier round of this project produced guards that could not fail
(a coverage assertion that was vacuous, a strip that was never exercised), and the
lesson recorded was to ask first whether a test has any way to be red. The probe
``_guardprobe_retention.py`` takes each guard away in turn and checks that the
matching test does go red.
"""

from __future__ import annotations

import asyncio
import gc
import json
import time
import weakref
from pathlib import Path
from typing import Any

import pytest

from app.graph.schema import RunCreateRequest, RunStatus, WorkflowGraph
from app.runtime.manager import REPLAY_TAIL, RunManager


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


def _linear(count: int = 0) -> WorkflowGraph:
    """``start`` → ``count`` prompt nodes → ``end``.

    Prompt nodes rather than LLM nodes because they are deterministic and
    hermetic; ``count`` is what lets a test produce a chosen number of events
    cheaply, since each node emits a started/finished pair.
    """
    ids = ["start", *(f"p{i}" for i in range(count)), "end"]
    nodes = [_node("start", "start", input_schema={})]
    nodes += [_node(f"p{i}", "prompt", template=f"step {i}") for i in range(count)]
    nodes.append(_node("end", "end", output_label="result"))
    return WorkflowGraph(
        id="wf-retention",
        nodes=nodes,
        edges=[_edge(a, b) for a, b in zip(ids, ids[1:])],
    )


def _pausable() -> WorkflowGraph:
    """A graph that stops at a Human node, leaving the run ``INTERRUPTED``."""
    return WorkflowGraph(
        id="wf-pause",
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


async def _execute(manager: RunManager, graph: WorkflowGraph) -> Any:
    """Create a graph and run it to a terminal state. Returns the context."""
    context = await manager.create(
        graph.id,
        graph,
        RunCreateRequest(workflow_id=graph.id, recursion_limit=200),
    )
    await manager.start(context)
    assert context.task is not None
    await context.task
    return context


@pytest.fixture
def manager() -> RunManager:
    return RunManager()


# ---------------------------------------------------------------------------
# 1. The live-only state is actually released
# ---------------------------------------------------------------------------


class TestFinishedRunsReleaseTheirLiveState:
    async def test_the_live_only_fields_are_dropped_when_the_run_ends(self, manager):
        context = await _execute(manager, _linear())

        assert context.status is RunStatus.SUCCEEDED
        assert context.checkpointer is None
        assert context.compiled is None
        assert context.graph is None

    async def test_release_and_retirement_agree(self, manager):
        """The field that makes a run evictable is set exactly when it is done.

        ``retired_at`` is the whole eviction policy — ``prune`` reads nothing
        else — so a run that finishes without setting it is immortal while every
        other assertion here still passes.
        """
        context = await _execute(manager, _linear())

        assert context.retired_at is not None
        assert not context.is_active

    async def test_a_run_that_has_not_started_is_not_retired(self, manager):
        """Consistency in the other direction: created but never started.

        It can still be started later, so it must not become droppable.
        """
        graph = _linear()
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id)
        )

        assert context.retired_at is None
        assert context.checkpointer is not None

    async def test_release_is_idempotent(self, manager):
        """A second call must not move the clock or half-clear anything.

        If it did, an accidental double call would extend the run's life instead
        of being a no-op.
        """
        context = await _execute(manager, _linear())
        first = context.retired_at

        context.release()

        assert context.retired_at == first


class TestTheReleaseFreesRatherThanDrops:
    """Reclaiming checkpoints means different things under the two providers.

    ===========================  ==============================  ==============
                                 per-run saver (``memory``)       shared saver (``sqlite``)
    ===========================  ==============================  ==============
    what ``release`` frees        the saver object itself          the reference only
    reclaimed by                  dropping the reference           eviction deleting rows
    ===========================  ==============================  ==============

    Both are correct, and the difference is the reason these tests name the
    provider they are exercising rather than inheriting the default. Making
    durable checkpoints the default turned the weakref assertion below red —
    **not because anything broke**, but because a shared saver is supposed to
    outlive any one run. A test that passes under only one provider should say so.
    """

    @pytest.fixture(autouse=True)
    def _per_run_saver(self):
        """Pin the provider this class is about, instead of relying on the default."""
        from app.runtime.checkpoints import MemoryCheckpointerProvider, set_checkpointer_provider

        set_checkpointer_provider(MemoryCheckpointerProvider())
        yield
        set_checkpointer_provider(None)

    async def test_no_reference_to_the_checkpointer_survives(self, manager, monkeypatch):
        """Setting the field to None is not the same as freeing the object.

        If anything else still holds the checkpointer — the compiled graph, a
        task, a closure over the emitter — the memory is not reclaimed and the
        leak is unchanged. Asserting ``context.checkpointer is None`` passes in
        that world; asserting the object is collectable does not.
        """
        from app.runtime.manager import RunContext

        seen: list[Any] = []
        original = RunContext.release

        def spy(self) -> None:
            seen.append(self.checkpointer)
            original(self)

        monkeypatch.setattr(RunContext, "release", spy)

        context = await _execute(manager, _linear())

        assert seen and seen[0] is not None, "release never ran"
        assert context.checkpointer is None

        reference = weakref.ref(seen[0])
        del seen[:]
        gc.collect()

        assert reference() is None, (
            "the checkpointer is still reachable after release, so its memory "
            "was never reclaimed"
        )

    async def test_the_shared_saver_is_expected_to_outlive_the_run(self, manager):
        """The counterpart, so the difference above is pinned rather than implied.

        Under a shared saver, dropping the reference reclaims nothing and that is
        correct — the saver belongs to the process. Saying so in a test stops the
        next reader from "fixing" the provider by making it per-run again, which
        would quietly put every run's checkpoints back into memory.
        """
        from app.runtime.checkpoints import (
            SqliteCheckpointerProvider,
            set_checkpointer_provider,
        )

        provider = SqliteCheckpointerProvider(manager_checkpoint_path())
        await provider.open()
        set_checkpointer_provider(provider)
        try:
            context = await _execute(manager, _linear())
            assert context.checkpointer is None, "release still drops the reference"
            # And the provider still holds it, because it is shared.
            assert provider.make() is not None
        finally:
            set_checkpointer_provider(None)
            await provider.close()


def manager_checkpoint_path():
    """A checkpoint database path inside this test's data directory."""
    from app.settings import get_settings

    return get_settings().data_dir / "checkpoints.db"


# ---------------------------------------------------------------------------
# 2. What must *not* be released
# ---------------------------------------------------------------------------


class TestRunsThatStillNeedTheirStateAreKept:
    async def test_an_interrupted_run_keeps_its_checkpointer(self, manager):
        """A paused run is not active, but it is not finished either.

        It is the one case where the checkpointer is load-bearing *after* the run
        stops, because ``resume`` cannot continue without it. A rule phrased as
        "release anything not currently running" would destroy it.
        """
        context = await _execute(manager, _pausable())

        assert context.status is RunStatus.INTERRUPTED
        assert context.checkpointer is not None
        assert context.compiled is not None
        assert context.retired_at is None

    async def test_an_interrupted_run_can_still_be_resumed(self, manager):
        """The observable consequence, rather than the internal flag."""
        context = await _execute(manager, _pausable())

        await manager.resume(context.id, {"approved": True, "values": {"value": "ok"}})
        assert context.task is not None
        await context.task

        assert context.status is RunStatus.SUCCEEDED
        assert context.outputs["result"] == {"value": "ok"}
        # …and once it really is finished it is released like any other run.
        assert context.checkpointer is None
        assert context.retired_at is not None


# ---------------------------------------------------------------------------
# 3. Eviction is bounded — by age and by count
# ---------------------------------------------------------------------------


class TestEvictionBounds:
    async def test_a_run_past_the_retention_window_is_dropped(self, manager):
        context = await _execute(manager, _linear())
        assert manager.get(context.id) is not None

        # The window is measured from retirement, and `now` is injectable so the
        # rule is testable without sleeping through it.
        dropped = manager.prune(now=time.monotonic() + 10_000)

        assert dropped == [context.id]
        assert manager.get(context.id) is None

    async def test_a_run_inside_the_window_is_kept(self, manager):
        """The partner test: without it, "prune always drops everything" passes."""
        context = await _execute(manager, _linear())

        assert manager.prune() == []
        assert manager.get(context.id) is not None

    async def test_the_count_cap_bounds_memory_inside_the_window(self, manager, settings):
        """Age alone bounds nothing under load.

        A thousand runs finishing inside one retention window are all young and
        would all be retained. The cap is what makes the bound unconditional.

        Asserted on the retained set rather than on a ``prune`` return value,
        because the cap is meant to hold *continuously*: ``_retire`` prunes after
        every run, so memory never depends on someone remembering to sweep.
        """
        settings.max_retained_runs = 3
        contexts = [await _execute(manager, _linear()) for _ in range(5)]

        # It must be the oldest that go, not whichever the dict happened to yield
        # first.
        assert set(manager.retained_ids()) == {c.id for c in contexts[-3:]}

    async def test_the_cap_does_not_reach_runs_that_are_paused(self, manager, settings):
        """A memory policy must never override correctness.

        With the cap at zero every finished run goes, but a run waiting on a
        human cannot be recreated from disk, so it stays regardless.
        """
        settings.max_retained_runs = 0
        finished = await _execute(manager, _linear())
        paused = await _execute(manager, _pausable())

        assert manager.get(finished.id) is None
        kept = manager.get(paused.id)
        assert kept is not None and kept.checkpointer is not None

    async def test_the_age_rule_does_not_reach_a_run_that_has_not_started(self, manager):
        graph = _linear()
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id)
        )

        assert context.retired_at is None
        assert manager.prune(now=time.monotonic() + 10_000) == []
        assert manager.get(context.id) is not None


# ---------------------------------------------------------------------------
# 4. Compacting memory must not lose the record
# ---------------------------------------------------------------------------


def _transcript_path(context: Any) -> Path:
    assert context.transcript_path is not None
    return Path(context.transcript_path)


class TestTheRecordSurvives:
    async def test_the_transcript_keeps_every_event_dropped_from_memory(self, manager):
        """The load-bearing ordering in ``_execute``'s ``finally``.

        ``_persist_finish`` writes the transcript and *then* ``_retire`` trims the
        bus. Swapping those two lines loses the beginning of every long run, and
        the only assertion that notices compares the transcript against what was
        actually emitted — so that is what this test does.
        """
        emitted: list[Any] = []
        context = await manager.create(
            "wf-retention",
            _linear(count=40),
            RunCreateRequest(workflow_id="wf-retention", recursion_limit=200),
        )

        async def consume() -> None:
            async for event in context.bus.subscribe():
                emitted.append(event)

        collector = asyncio.create_task(consume())
        await manager.start(context)
        assert context.task is not None
        await context.task
        await collector

        # Precondition: the run is long enough for trimming to bite at all.
        assert len(emitted) > REPLAY_TAIL

        transcript = json.loads(_transcript_path(context).read_text(encoding="utf-8"))

        assert len(transcript["events"]) == len(emitted)
        assert [e["seq"] for e in transcript["events"]] == [e.seq for e in emitted]
        # While memory holds only the tail.
        assert len(context.bus.history()) <= REPLAY_TAIL

    async def test_the_steps_are_still_readable_after_the_context_is_evicted(self, manager):
        """Eviction drops a cache entry, not the history."""
        from sqlmodel import select

        from app.db.models import Run as RunRow
        from app.db.models import RunStep as RunStepRow
        from app.db.session import session_scope

        context = await _execute(manager, _linear(count=3))
        assert context.step_order

        manager.prune(now=time.monotonic() + 10_000)

        with session_scope() as session:
            assert session.get(RunRow, context.id) is not None
            rows = session.exec(
                select(RunStepRow).where(RunStepRow.run_id == context.id)
            ).all()
            assert len(rows) == len(context.step_order)


# ---------------------------------------------------------------------------
# 5. The narrowed replay window is reported, not hidden
# ---------------------------------------------------------------------------


class TestTheReplayWindowIsHonest:
    def test_trimming_keeps_the_newest_events(self):
        from app.graph.schema import RunEventType
        from app.runtime.events import EventBus

        bus = EventBus("r1")

        async def fill() -> None:
            for i in range(10):
                await bus.emit(RunEventType.LOG, data={"i": i})

        asyncio.run(fill())
        assert bus.trim(4) == 6

        assert [e.data["i"] for e in bus.history()] == [6, 7, 8, 9]
        # The gap is observable rather than implied.
        assert bus.earliest_seq == 7
        assert bus.last_seq == 10

    def test_trimming_beyond_the_retained_count_changes_nothing(self):
        """Trim must be a no-op when nothing needs dropping.

        Without this, an implementation that reported a gap unconditionally would
        look correct whenever a test only ever trimmed a long bus.
        """
        from app.graph.schema import RunEventType
        from app.runtime.events import EventBus

        bus = EventBus("r1")

        async def fill() -> None:
            for _ in range(3):
                await bus.emit(RunEventType.LOG, data={})

        asyncio.run(fill())

        assert bus.trim(10) == 0
        assert bus.earliest_seq == 1
        assert len(bus.history()) == 3

    def test_the_retained_tail_goes_back_into_the_right_buffer(self):
        """An internal invariant, asserted directly and deliberately.

        The bus keeps ordinary events and chunk events in two lists with very
        different caps, because chunks are bulk traffic. ``trim`` has to put the
        tail back where it found it; a version that dumped everything into the
        ordinary list would look perfect today — ``history`` merges both — and
        leave the chunk buffer empty, so the next ``emit`` would restart
        unbounded chunk accumulation, which is the exact failure this whole change
        exists to prevent. Nothing public reveals that, so the assertion is on the
        buffer itself.
        """
        from app.graph.schema import RunEventType
        from app.runtime.events import EventBus

        bus = EventBus("r1")

        async def fill() -> None:
            for i in range(6):
                await bus.emit(RunEventType.NODE_OUTPUT_CHUNK, data={"i": i})
                await bus.emit(RunEventType.LOG, data={"i": i})

        asyncio.run(fill())
        assert bus.trim(4) == 8

        remaining = bus.history()
        assert [(e.type, e.data["i"]) for e in remaining] == [
            (RunEventType.NODE_OUTPUT_CHUNK, 4),
            (RunEventType.LOG, 4),
            (RunEventType.NODE_OUTPUT_CHUNK, 5),
            (RunEventType.LOG, 5),
        ]
        assert bus.earliest_seq == 9
        assert bus._chunk_history == [
            e for e in remaining if e.type is RunEventType.NODE_OUTPUT_CHUNK
        ]
        assert bus._history == [
            e for e in remaining if e.type is not RunEventType.NODE_OUTPUT_CHUNK
        ]

    async def test_a_reconnect_after_the_run_ends_still_receives_the_ending(self, manager):
        """Why a tail is kept rather than the history dropped outright.

        A client whose connection dropped just before the run finished reconnects
        and must still be told how it ended. Trimming to nothing would leave that
        client waiting for a terminal event that can never come.
        """
        context = await _execute(manager, _linear(count=40))

        assert context.bus.earliest_seq > 1, "expected a trimmed bus"

        replayed = [event async for event in context.bus.subscribe(after=0)]

        assert replayed
        assert replayed[-1].type.value == "run_finished"
        assert replayed[0].seq == context.bus.earliest_seq


# ---------------------------------------------------------------------------
# 6. Eviction must not be visible as a spurious 404
# ---------------------------------------------------------------------------


@pytest.fixture
def app_manager(monkeypatch) -> RunManager:
    """Point the process-wide manager at a fresh instance for one test."""
    from app.runtime import manager as manager_module

    local = RunManager()
    monkeypatch.setattr(manager_module, "_manager", local)
    return local


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    # A loopback peer keeps AUTH_MODE=local valid, matching the other API tests.
    return TestClient(app, client=("127.0.0.1", 51234))


class TestEvictedRunsStillAnswerOverHttp:
    async def test_a_get_falls_back_to_the_database(self, app_manager):
        """Bounding memory must not break the API.

        This is the regression eviction introduces if the read path is left
        alone: ``/runs/{id}`` 404'd as soon as the context was gone, even though
        the run's row and steps were sitting in the database. The response shape
        must also stay identical to the in-memory one.
        """
        context = await _execute(app_manager, _linear(count=2))
        app_manager.prune(now=time.monotonic() + 10_000)
        assert app_manager.get(context.id) is None

        with _client() as client:
            response = client.get(f"/api/runs/{context.id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == context.id
        assert payload["status"] == RunStatus.SUCCEEDED.value
        assert [step["node_id"] for step in payload["steps"]] == context.step_order

    async def test_the_fallback_does_not_invent_runs(self, app_manager):
        """The partner test.

        A fallback that answered 200 for anything would satisfy the test above
        and be far worse than the 404 it replaced.
        """
        with _client() as client:
            response = client.get("/api/runs/does-not-exist")

        assert response.status_code == 404

    async def test_the_stream_distinguishes_gone_from_unknown(self, app_manager):
        """410 tells the client to read history; 404 tells it there is a bug.

        Collapsing the two into one status is how "your run was evicted" becomes
        half an hour spent looking for a typo in the run id.
        """
        context = await _execute(app_manager, _linear())
        app_manager.prune(now=time.monotonic() + 10_000)

        with _client() as client:
            gone = client.get(f"/api/runs/{context.id}/events")
            unknown = client.get("/api/runs/does-not-exist/events")

        assert gone.status_code == 410
        assert "no longer in memory" in gone.json()["detail"]
        assert unknown.status_code == 404

    async def test_the_event_stream_reports_the_replay_window(self, app_manager):
        """The header exists so "lossless" is never claimed falsely.

        Nothing inside the stream says whether it replayed the whole run or only
        the tail, so without this a client cannot tell a complete replay from a
        truncated one — and would render the truncation as the whole story.

        Three assertions, because the first two alone are unfalsifiable: comparing
        the header against ``bus.earliest_seq`` passes even if that property is
        hard-coded to 1, since both sides would be wrong together. Anchoring the
        header against the events actually retained is what makes the value
        falsifiable.
        """
        context = await _execute(app_manager, _linear(count=40))

        with _client() as client:
            with client.stream("GET", f"/api/runs/{context.id}/events") as stream:
                reported = int(stream.headers["x-replay-from"])

        assert reported == context.bus.earliest_seq
        # A run this long was definitely trimmed, so a replay window of 1 would
        # be a claim that the whole run is available — which it is not.
        assert reported > 1
        assert context.bus.history()[0].seq == reported

    async def test_the_two_summary_paths_have_the_same_shape(self, app_manager):
        """One run must have one shape on the wire, whichever path answered."""
        from app.api.runs import _persisted_summary, _run_summary

        context = await _execute(app_manager, _linear(count=2))
        live_keys = set(_run_summary(context, include_steps=True))

        app_manager.prune(now=time.monotonic() + 10_000)
        dead_keys = set(_persisted_summary(context.id, include_steps=True))

        assert live_keys == dead_keys


# ---------------------------------------------------------------------------
# 7. The frame separator is a contract with two hand-written parsers
# ---------------------------------------------------------------------------


class TestTheWireFormatIsWhatTheClientParses:
    """`event:`/`id:`/`data:` on LF, blank-line separated — asserted on bytes.

    This is here because the format broke silently the moment the hand-written
    frame encoder was replaced with `sse-starlette`. Two things conspire:

    * the library's default separator is CRLF, and
    * `EventSourceResponse(sep=...)` does **not** reach events handed to it
      already built, so pinning the response alone leaves the events on CRLF.

    The consumers are both hand-written and both split on ``"\\n\\n"``, which a
    CRLF stream never contains. The failure was therefore invisible: the
    connection stayed open, every event was delivered, and the canvas rendered
    nothing at all. Asserting on parsed events would not have caught it — they
    parse fine either way — so this reads the raw bytes.
    """

    async def test_frames_use_lf_and_carry_the_fields_the_clients_read(
        self, app_manager
    ):
        context = await _execute(app_manager, _linear(count=2))

        with _client() as client:
            with client.stream("GET", f"/api/runs/{context.id}/events") as stream:
                raw = b"".join(stream.iter_bytes())

        assert b"\r" not in raw, (
            "the stream is CRLF-terminated. `frontend/src/api/client.ts` splits "
            "frames on '\\n\\n' and would find none, rendering nothing."
        )
        frames = [frame for frame in raw.decode("utf-8").split("\n\n") if frame.strip()]
        assert frames, "the stream carried no frames"

        for frame in frames:
            lines = frame.split("\n")
            assert any(line.startswith("data: ") for line in lines), frame
            # `scripts/smoke.py` keeps exactly the lines starting with "data: "
            # and json-decodes the remainder, so the space after the colon is
            # part of the contract rather than a style choice.
            payload = next(line[6:] for line in lines if line.startswith("data: "))
            assert json.loads(payload)["type"]
            assert any(line.startswith("id: ") for line in lines), frame
            assert any(line.startswith("event: ") for line in lines), frame

    async def test_the_reconnect_header_is_still_read(self, app_manager):
        """`Last-Event-ID` was a plain parameter, so FastAPI bound it to the query
        string and the standard header was ignored — a docstring claiming support
        for a reconnect path that did not exist. Pinned because the fix is one
        keyword and silently reversible."""
        context = await _execute(app_manager, _linear(count=5))
        expected = context.bus.earliest_seq

        with _client() as client:
            with client.stream(
                "GET",
                f"/api/runs/{context.id}/events",
                headers={"Last-Event-ID": "3"},
            ) as stream:
                first = next(iter(stream.iter_lines()))

        assert first.startswith("id: "), first
        assert int(first[4:]) >= 4, "the header was ignored and replay started at 0"
        assert expected >= 1


# ---------------------------------------------------------------------------
# 8. Consistency fix found while doing the above
# ---------------------------------------------------------------------------


class TestATerminalPathSetsBothClocks:
    async def test_cancel_records_a_monotonic_end_time(self, manager):
        """``duration_ms`` falls back to ``time.perf_counter()`` when the
        monotonic end is unset, so a terminal path that set only the wall-clock
        ``finished_at`` left ``duration_ms`` growing on every read — a live-looking
        number on a dead run.

        ``cancel`` was the one terminal path that did this, in the branch where
        there is no running task to cancel. That branch is hard to reach over
        HTTP, which is exactly why nobody noticed: the invariant is worth holding
        even where reachability is thin. The test sets up the precondition
        directly rather than pretending a race can be forced.
        """
        graph = _linear()
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id)
        )
        # As if it had been running for a second.
        context.status = RunStatus.RUNNING
        context.started_at_monotonic = time.perf_counter() - 1.0

        await manager.cancel(context.id)
        first = context.duration_ms

        assert context.status is RunStatus.CANCELLED
        assert context.finished_at_monotonic is not None
        assert first is not None and first >= 1000.0

        time.sleep(0.01)
        assert context.duration_ms == first

    async def test_a_cancelled_run_is_retired(self, manager):
        """Cancel is a terminal path too, so it must release like the others."""
        graph = _linear()
        context = await manager.create(
            graph.id, graph, RunCreateRequest(workflow_id=graph.id)
        )

        await manager.cancel(context.id)

        assert context.retired_at is not None
        assert context.checkpointer is None
