"""Run orchestration.

Responsibilities, in order of importance:

1. **Execute a compiled workflow** and translate the engine's progress into
   typed events the UI can render, without the workflow author writing any
   plumbing.
2. **Persist what happened** — a ``Run`` row, one ``RunStep`` per node execution,
   and a full event transcript on disk.
3. **Support human-in-the-loop** by holding the checkpointer and thread id needed
   to resume an interrupted run.

Runs execute as background asyncio tasks so the HTTP request that started them
returns immediately. The client then follows the run over SSE.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.graph.compiler import CompileError, compile_graph
from app.graph.schema import (
    RunCreateRequest,
    RunEventType,
    RunStatus,
    RunStepStatus,
    WorkflowGraph,
)
from app.runtime.checkpoints import get_checkpointer_provider
from app.runtime.events import EventBus
from app.runtime.runs import get_run_store
from app.runtime.serialize import jsonable, redact
from app.runtime.budget import (
    Budget,
    BudgetExceeded,
    clear_budget,
    register_budget,
)
from app.runtime.projection import WindDown
from app.runtime.queue import RunCancelledWhileQueued, get_task_queue
from app.runtime.workflows import WorkflowUnavailable, load_workflow
from app.settings import get_settings

#: Maps the compiler's emitter kinds onto wire event types.
_KIND_TO_EVENT: dict[str, RunEventType] = {
    "node_started": RunEventType.NODE_STARTED,
    "node_finished": RunEventType.NODE_FINISHED,
    "node_failed": RunEventType.NODE_FAILED,
    "chunk": RunEventType.NODE_OUTPUT_CHUNK,
    "log": RunEventType.LOG,
}

#: How many events to keep for replay once a run has finished.
#:
#: The bus retains a large replay buffer *while a run is live*, because a client
#: may connect or reconnect mid-run and must be able to catch up. That reason
#: disappears the moment the run ends: the only thing a late subscriber still
#: needs is the ending — the terminal event, plus enough context to make sense of
#: it. Everything before that is already in the transcript on disk and in the
#: ``RunStep`` rows, so keeping thousands of events per finished run buys nothing
#: and costs the most memory on the busiest days.
REPLAY_TAIL = 64

#: Most log lines kept per step.
#:
#: The step list is a summary of what a node did, not its stdout. A Code node
#: that prints inside a loop would otherwise put an unbounded list in a run's
#: transcript, and the transcript is loaded on every `GET /runs/{id}`.
MAX_STEP_LOGS = 100

logger = logging.getLogger(__name__)


class RunError(RuntimeError):
    """Raised for problems starting or controlling a run."""


#: Distinguishes "resume with the value None" from "start fresh". A human may
#: legitimately answer with null, so `None` cannot double as the sentinel.
_UNSET: Any = object()


@dataclass
class StepRecord:
    """In-memory accumulator for one node execution."""

    node_id: str
    node_label: str
    index: int
    status: str = RunStepStatus.RUNNING.value
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: Lines the node emitted with `ctx.log(...)`.
    #:
    #: Kept because they were being thrown away. The emitter sent them to the
    #: live event stream and the recorder had no branch for them, so a node's
    #: own explanation of what it did survived only until the client
    #: disconnected — `GET /runs/{id}/steps` returned `logs: None` for every
    #: step of every run. Measured: a `memory` node logs unconditionally and its
    #: line was absent from the step list.
    #:
    #: Bounded for the same reason everything else here is: a Code node can print
    #: in a loop, and an unbounded per-step list is unbounded memory per run.
    logs: list[str] = field(default_factory=list)


@dataclass
class RunContext:
    """Everything the manager needs to know about one run."""

    id: str
    workflow_id: str
    graph: WorkflowGraph | None
    inputs: dict[str, Any]
    tweaks: dict[str, dict[str, Any]]
    recursion_limit: int
    thread_id: str

    status: RunStatus = RunStatus.PENDING
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    bus: EventBus = field(init=False)
    #: Live-only state. Both are set to ``None`` by :meth:`release`.
    #:
    #: Resolved from the process provider rather than constructed here, so that a
    #: deployment configured for durable checkpoints gets the shared saver. Under
    #: the default (memory) provider this still produces a fresh `InMemorySaver`
    #: per run, exactly as before.
    checkpointer: BaseCheckpointSaver | None = field(
        default_factory=lambda: get_checkpointer_provider().make()
    )
    compiled: Any = None
    task: asyncio.Task[None] | None = None

    #: Monotonic timestamp of the moment this run became droppable, or ``None``
    #: while it can still make progress. This single field is the whole eviction
    #: policy: it is set by :meth:`release` and read by :meth:`RunManager.prune`.
    #:
    #: It is deliberately *not* derived from ``status``. An interrupted run is not
    #: active, but it is also not droppable — a human still owns it and ``resume``
    #: needs its checkpointer. A run created but never started is in the same
    #: position. Expressed as one nullable field, "can this be dropped?" has one
    #: answer in one place instead of a status list every call site must repeat.
    retired_at: float | None = None

    #: Present when the run is paused awaiting a human.
    interrupt: dict[str, Any] | None = None
    #: What this run may spend and what it has spent. Attached to the context so
    #: the compiler's per-node check and the manager's accounting share one
    #: object — see `runtime/budget.py`.
    budget: Budget = field(default_factory=Budget)
    #: Steps keyed by node id. A node that runs more than once (a loop body)
    #: keeps the latest entry, which is what the UI shows.
    steps: dict[str, StepRecord] = field(default_factory=dict)
    step_order: list[str] = field(default_factory=list)
    step_counter: int = 0

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    started_at_monotonic: float | None = None
    finished_at_monotonic: float | None = None
    transcript_path: str | None = None

    def __post_init__(self) -> None:
        self.bus = EventBus(self.id)

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration, measured monotonically so it is immune to
        system clock adjustments."""
        if self.started_at_monotonic is None:
            return None
        end = self.finished_at_monotonic
        if end is None:
            end = time.perf_counter()
        return round((end - self.started_at_monotonic) * 1000, 1)

    @property
    def is_active(self) -> bool:
        return self.status in (RunStatus.PENDING, RunStatus.RUNNING)

    def release(self) -> None:
        """Drop the state that only a *live* run needs, and mark it droppable.

        Called once the run can no longer make progress, immediately after its
        transcript has been written. What goes, and why it is safe:

        * **``checkpointer``** — the dominant cost. LangGraph's in-memory
          checkpointer keeps *every* checkpoint (one per super-step) for the life
          of the process, so it is unbounded in two directions at once: runs ×
          super-steps. Nothing reads it after a terminal event; only ``resume``
          does, and a resumed run is by definition not terminal.
        * **``compiled``** — the compiled graph object, rebuilt from ``graph`` in
          microseconds if a run were ever restarted (it is not).
        * **``graph``** — the workflow definition, which is also in SQLite. It was
          only ever read while *compiling*.
        * **the bus's replay history** — trimmed to the last few events rather
          than dropped entirely, so a client that reconnects at the moment the
          run ends still receives the terminal event it missed.

        Idempotent, so a double call cannot leave a half-released context.
        """
        if self.retired_at is None:
            self.retired_at = time.monotonic()
        self.compiled = None
        self.checkpointer = None
        self.graph = None
        self.bus.trim(REPLAY_TAIL)


class RunManager:
    """Process-wide registry of runs.

    State lives in memory, which is the right trade-off for a local dev tool: a
    run's identity is its ``thread_id``, and the live objects — the compiled graph,
    the event bus, the task — are not meaningfully serialisable anyway. The
    ``Run`` and ``RunStep`` rows are written regardless, so history is never lost.

    A paused run is the one case where "lives in memory" was not good enough, and
    the shape of the fix is worth knowing before reading the methods below:

    * the **state** survives a restart because the checkpointer is durable
      (``CHECKPOINT_STORE=sqlite``, the default — see ``runtime/checkpoints.py``);
    * the **context** is rebuilt on demand by :meth:`RunManager.hydrate`, from the
      ``Run`` row plus the ``Workflow`` definition plus the checkpoint;
    * :meth:`RunManager.resume` falls back to it when the run is not in memory.

    Durable state without a rebuild was the state of this system for one task:
    the checkpoints were on disk, the row still said ``interrupted``, and Resume
    answered "unknown run".
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunContext] = {}
        #: Guards the register-and-persist step of `create`, so two overlapping
        #: requests cannot interleave a compile with a write of the same id.
        #: Held across the awaits in that method, which is the only reason it is
        #: a lock at all — the dict assignment itself is already atomic.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ create
    async def create(
        self, workflow_id: str, graph: WorkflowGraph, request: RunCreateRequest
    ) -> RunContext:
        """Compile and register a run, then start it in the background."""
        settings = get_settings()

        # Compiling here (rather than inside the task) means a broken workflow
        # fails the HTTP request that asked for the run, where the user can see
        # the reason — instead of failing silently in the background.
        run_id = _new_id()
        context = RunContext(
            id=run_id,
            workflow_id=workflow_id,
            graph=graph,
            inputs=dict(request.inputs or {}),
            tweaks=dict(request.tweaks or {}),
            recursion_limit=request.recursion_limit or settings.recursion_limit,
            thread_id=run_id,
            budget=Budget.from_settings(settings),
        )

        try:
            context.compiled = compile_graph(
                graph,
                checkpointer=context.checkpointer,
                emitter=self._make_emitter(context),
            )
        except CompileError as exc:
            raise RunError(str(exc)) from exc

        # Registering and recording the row are one critical section: both touch
        # state a concurrent `create` would race on, and a crash between them
        # would leave a run that exists in memory with no row (or the reverse).
        async with self._lock:
            self._runs[run_id] = context
            register_budget(run_id, context.budget)
            self._persist_start(context)
        return context

    async def start(self, context: RunContext) -> None:
        """Queue the run for execution and return immediately.

        The task is created here so the HTTP request that asked for the run can
        return its id at once, and the *waiting* happens inside it. Blocking the
        request on a free slot would turn "the platform is busy" into a request
        that hangs, which is the opposite of what a queue is for.
        """
        context.task = asyncio.create_task(self._queued_execute(context))

    async def _queued_execute(self, context: RunContext) -> None:
        """Wait for a free slot, then run. The one place the queue is consulted."""
        queue = get_task_queue()

        if not queue.try_acquire(context.id):
            # Only now is it true that this run is waiting, so only now does it
            # say so. A run that started immediately never sees `queued`.
            context.status = RunStatus.QUEUED
            await context.bus.emit(
                RunEventType.RUN_QUEUED,
                data={"position": queue.position(context.id), "limit": queue.limit},
            )
            try:
                await queue.acquire(context.id)
            except RunCancelledWhileQueued:
                # Dropped while waiting. Nothing has run, so there is nothing to
                # unwind — only a status to record and a client to tell.
                context.status = RunStatus.CANCELLED
                context.error = "Run was cancelled before it started."
                context.finished_at = datetime.now(timezone.utc)
                context.finished_at_monotonic = time.perf_counter()
                await context.bus.emit(
                    RunEventType.RUN_FAILED,
                    data={"error": context.error, "cancelled": True},
                )
                context.bus.close()
                self._persist_finish(context)
                self._retire(context)
                queue.release(context.id)
                return

        try:
            await self._execute(context)
        finally:
            queue.release(context.id)

    # --------------------------------------------------------------- execute

    # --------------------------------------------------------------- execute
    async def _execute(self, context: RunContext, *, resume_value: Any = _UNSET) -> None:
        """Run the compiled graph and record everything that happens."""
        context.status = RunStatus.RUNNING
        context.started_at_monotonic = time.perf_counter()
        context.finished_at_monotonic = None
        context.finished_at = None
        context.interrupt = None

        await context.bus.emit(
            RunEventType.RUN_STARTED,
            data={"workflow_id": context.workflow_id, "inputs": redact(context.inputs)},
        )

        config: dict[str, Any] = {
            "configurable": {
                "thread_id": context.thread_id,
                "tweaks": context.tweaks,
                # How a node finds the run's budget. See `runtime/budget.py` for
                # why this is a registry lookup rather than an object smuggled
                # through `configurable`.
                "run_id": context.id,
            },
            "recursion_limit": context.recursion_limit,
        }

        try:
            payload: Any
            if resume_value is not _UNSET:
                from langgraph.types import Command

                payload = Command(resume=resume_value)
            else:
                payload = {"inputs": context.inputs}

            result = await context.compiled.ainvoke(payload, config=config)

            state, interrupts = _split_result(result)

            if interrupts:
                context.status = RunStatus.INTERRUPTED
                context.interrupt = interrupts[0]
                await context.bus.emit(
                    RunEventType.INTERRUPT_RAISED,
                    data={"interrupt": context.interrupt},
                )
            else:
                context.status = RunStatus.SUCCEEDED
                context.outputs = jsonable(state.get("output") or {})
                await context.bus.emit(
                    RunEventType.RUN_FINISHED,
                    data={"outputs": context.outputs, "duration_ms": context.duration_ms},
                )

        except asyncio.CancelledError:
            context.status = RunStatus.CANCELLED
            context.error = "Run was cancelled."
            await context.bus.emit(
                RunEventType.RUN_FAILED, data={"error": context.error, "cancelled": True}
            )
            raise

        except BudgetExceeded as exc:
            # Its own branch because the run did not break — it was stopped on
            # purpose, and the message has to say which limit and by how much.
            # Folding it into the generic handler would lose that.
            context.status = RunStatus.FAILED
            context.error = str(exc)
            if context.budget.keeps_partial_result:
                # `BUDGET_EXIT_BEHAVIOR=keep`: stopping is about not spending
                # more, not about discarding what was already produced. The status
                # stays `failed` — a run that hit its limit did not complete, and
                # reporting it as success would let a caller treat a truncated
                # answer as the answer.
                await self._capture_partial_output(context)
            await context.bus.emit(
                RunEventType.RUN_FAILED,
                data={
                    "error": context.error,
                    "type": "BudgetExceeded",
                    "limit": exc.limit_name,
                    "usage": context.budget.usage(),
                },
            )

        except WindDown as exc:
            # A third outcome, and the reason the exception is separate: the run
            # was steered to a controlled stop while it still had allowance. It is
            # **not** a failure — nothing broke and no ceiling was crossed — so
            # reporting `failed` would tell a caller their workflow was broken
            # when it was managed. It is not `succeeded` either, or a truncated
            # answer would be indistinguishable from a complete one.
            context.status = RunStatus.STOPPED
            context.error = str(exc)
            # The node that triggered the stop already finished, so its output is
            # in the state; taking the partial result is what makes "finish with
            # what you have" mean anything.
            await self._capture_partial_output(context)
            await context.bus.emit(
                RunEventType.RUN_FINISHED,
                data={
                    "outputs": context.outputs,
                    "stopped": True,
                    "reason": str(exc),
                    "usage": context.budget.usage(),
                },
            )

        except Exception as exc:  # noqa: BLE001 - every failure must reach the UI
            context.status = RunStatus.FAILED
            context.error = _explain(exc)
            await context.bus.emit(
                RunEventType.RUN_FAILED,
                data={"error": context.error, "type": type(exc).__name__},
            )

        finally:
            context.finished_at = datetime.now(timezone.utc)
            context.finished_at_monotonic = time.perf_counter()
            context.bus.close()
            # Written even for a stopped run: "which node spent the money" is
            # answerable only from the steps and the transcript, and a budget that
            # discards its own evidence is not useful for tuning the next run.
            self._persist_finish(context)
            # Ordering matters: `_persist_finish` writes the transcript, which
            # reads the bus history that `release` then discards.
            self._retire(context)
            await self._forget_checkpoints(context)
            # Dropped last, so a late reader (the API) can still report usage.
            clear_budget(context.id)
            # After the transcript is written, so a failed auto-write cannot cost
            # the run's own evidence. Never raises — see the method.
            await self._remember_if_asked(context)

    async def _remember_if_asked(self, context: RunContext) -> None:
        """Write this run's result into long-term memory, when configured to.

        **Task 20's explicit answer to "what is worth remembering".** The node in
        `app/memory/nodes.py` is the *explicit* half: the graph says "remember
        this", and an author draws that edge. This is the automatic half, and it
        is deliberately narrow rather than clever.

        What it records: the run's **output**, when the run succeeded. Not the
        whole transcript, not a model's judgement of what mattered — those are the
        parts with no measured answer yet. An output is a fact the agent produced
        and could be asked about again, which is the smallest thing that is
        useful, and it is one `aput` with the same content-addressed key the
        memory node uses, so the same output twice overwrites instead of piling
        up copies.

        Off by default (`MEMORY_AUTO_WRITE=false`). Every run writing a row is a
        decision with a cost — the store grows, and retrieval quality drops as
        near-duplicates accumulate — so it is asked for rather than assumed.

        Never raises. It runs in a `finally` after a run has already finished and
        been recorded, and a memory failure at that point must not turn a
        successful run into a failed one.
        """
        settings = get_settings()
        if not settings.memory_auto_write or context.status is not RunStatus.SUCCEEDED:
            return

        from app.memory.store import get_memory_store

        store = get_memory_store()
        if store is None:
            # `MEMORY_STORE=off` is a supported configuration, not a fault.
            logger.debug("Auto-write skipped: no memory store is configured.")
            return

        text = (context.outputs or {}).get("text") or ""
        if not isinstance(text, str) or not text.strip():
            # Nothing quotable. A run whose output is a number or a structure is
            # not something the memory node could retrieve usefully either.
            return

        try:
            from app.memory.nodes import _derive_key

            namespace = ("agent", context.workflow_id)
            await store.aput(
                namespace,
                _derive_key(text),
                {"text": text, "run_id": context.id, "auto": True},
            )
            logger.info("Run %s remembered its output.", context.id)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Auto-write failed for run %s: %s", context.id, exc)

    async def _capture_partial_output(self, context: RunContext) -> None:
        """Record what the graph had produced before the run was stopped.

        Reads LangGraph's own state snapshot rather than tracking outputs in the
        manager, because the engine already holds exactly this and a second
        bookkeeping path is the failure this project has hit three times.

        Never raises: this runs while handling a budget stop, and failing to
        enrich the result must not replace the real reason the run ended.
        """
        try:
            snapshot = await self._read_snapshot(context)
            values = getattr(snapshot, "values", None) or {}
            output = values.get("output")
            if output:
                # An End node ran, so there is a finished result to report.
                context.outputs = jsonable(output)
            else:
                # Nothing reached an End node: report the per-node outputs, which
                # is what a caller can actually use to see how far it got.
                variables = values.get("vars") or {}
                context.outputs = jsonable(
                    {"partial": True, "vars": variables}
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not read the partial state of run %s", context.id)

    async def _forget_checkpoints(self, context: RunContext) -> None:
        """Delete the checkpoints of a run that has reached a terminal state.

        Dropping the reference in :meth:`RunContext.release` is the whole cleanup
        under the per-run memory provider — the object *was* the storage. Under a
        shared durable saver it is not: the rows outlive the reference, so leaving
        them would trade an in-memory leak for a disk leak.

        An ``INTERRUPTED`` run is skipped, and that skip is the point of the whole
        feature: those checkpoints are the only thing a ``resume`` can read.
        ``release`` already declines to touch such a run, so this mirrors a
        decision made in one place rather than inventing a second one.
        """
        if context.status is RunStatus.INTERRUPTED:
            return
        try:
            await get_checkpointer_provider().forget(context.thread_id)
        except Exception:  # noqa: BLE001
            # Same reasoning as persistence: failing to tidy up must not turn a
            # successful run into a failed one. It is worth a log line, though.
            logger.exception("Failed to drop checkpoints for run %s", context.id)

    # ---------------------------------------------------------------- emitter
    def _make_emitter(self, context: RunContext):
        """Build the ``emit(kind, data, node_id, node_label)`` callback.

        This is the bridge between the compiler (which knows about nodes) and the
        event bus (which knows about clients).
        """

        async def emitter(
            kind: str, data: dict[str, Any], node_id: str, node_label: str
        ) -> None:
            event_type = _KIND_TO_EVENT.get(kind)
            if event_type is None:
                return

            # Model nodes report what they spent by emitting `{"usage": ...}` —
            # both the LLM and Agent node do this. Accumulating from the event
            # rather than from the node's return value keeps the budget
            # independent of every node's output shape, and means a future node
            # that calls a model is accounted for without touching the compiler.
            if kind == "chunk" and data.get("usage") is not None:
                context.budget.add_usage(data.get("usage"))

            self._track_step(context, kind, data, node_id, node_label)

            await context.bus.emit(
                event_type, node_id=node_id, node_label=node_label, data=data
            )

        return emitter

    def _track_step(
        self,
        context: RunContext,
        kind: str,
        data: dict[str, Any],
        node_id: str,
        node_label: str,
    ) -> None:
        if kind == "node_started":
            # Counted here rather than in the compiler so "steps" means the same
            # thing to the budget and to the timeline: one entry per node
            # execution, including a loop body that runs several times.
            context.budget.add_step()
            if node_id not in context.steps:
                context.step_counter += 1
                context.step_order.append(node_id)
            previous = context.steps.get(node_id)
            context.steps[node_id] = StepRecord(
                node_id=node_id,
                node_label=node_label,
                index=context.step_counter,
                inputs=data.get("inputs") or {},
                # Carried over, not reset: a log line can arrive before this
                # event (see the `log` branch below), and starting a fresh record
                # would drop it. Re-assigning rather than mutating is deliberate —
                # a loop body's second pass must not inherit the first pass's
                # outputs.
                logs=list(previous.logs) if previous is not None else [],
            )
            return

        record = context.steps.get(node_id)
        if record is None:
            if kind != "log":
                # Every other kind is about a node that must already have
                # started; a record arriving without one means the emitter and
                # the recorder disagree about order, and inventing a record would
                # hide that.
                return
            # A `log` can legitimately arrive before `node_started`.
            #
            # Measured: the compiler's budget forecast emits its warning *before*
            # the node runs — that is the point of a forecast — so the line was
            # dropped by this early return and never appeared anywhere. Rather
            # than reorder the compiler (the warning has to come before the work),
            # a step record is opened for the line. `node_started` will find it
            # and fill in the rest.
            if node_id not in context.steps:
                context.step_counter += 1
                context.step_order.append(node_id)
            record = StepRecord(
                node_id=node_id,
                node_label=node_label,
                index=context.step_counter,
            )
            context.steps[node_id] = record

        if kind == "node_finished":
            record.status = RunStepStatus.SUCCEEDED.value
            record.outputs = data.get("outputs") or {}
            record.duration_ms = data.get("duration_ms")
        elif kind == "node_failed":
            record.status = RunStepStatus.FAILED.value
            record.error = data.get("error")
            # A node that failed but continued still produced output, and the
            # timeline has to show it: "this step failed and here is what it
            # contributed instead" is the whole point of `on_error=default`.
            # Without this the row said `failed` with null outputs, which reads
            # as "produced nothing" rather than "produced the fallback".
            if data.get("outputs") is not None:
                record.outputs = data["outputs"]
            record.duration_ms = data.get("duration_ms")
        elif kind == "log":
            # Appended here so the line is in the transcript rather than only in
            # the live event stream — see `StepRecord.logs` for what was wrong.
            message = data.get("message")
            if message:
                level = str(data.get("level") or "info")
                # The level is prefixed rather than kept as a field: the step list
                # renders a plain list of strings, and a warning that looks
                # identical to an info line is the failure mode worth avoiding.
                marker = "" if level == "info" else f"[{level}] "
                if len(record.logs) < MAX_STEP_LOGS:
                    record.logs.append(f"{marker}{message}")
                elif len(record.logs) == MAX_STEP_LOGS:
                    record.logs.append(
                        f"(further log lines omitted; the first {MAX_STEP_LOGS} are shown)"
                    )

    # ----------------------------------------------------------------- resume
    async def resume(self, run_id: str, value: Any) -> RunContext:
        """Continue an interrupted run with the human's answer.

        Looks the run up in memory first, and **rebuilds it from persistence when
        it is not there** — which is the case after a restart, and the reason
        durable checkpoints alone were not enough. Before this fallback, a paused
        run survived a restart as a row and a checkpoint that nothing could use:
        the endpoint answered "unknown run".
        """
        context = self._runs.get(run_id)
        if context is None:
            context = await self.hydrate(run_id)
        if context.status is not RunStatus.INTERRUPTED:
            raise RunError(
                f"Run '{run_id}' is {context.status.value}, not awaiting input."
            )
        if context.checkpointer is None:
            # Defensive: an interrupted run is exempt from eviction precisely so
            # this cannot happen, and `hydrate` refuses when the checkpoints are
            # gone. If it ever does, say so plainly rather than failing later
            # inside LangGraph.
            raise RunError(
                f"Run '{run_id}' has no checkpoints to resume from."
            )
        if context.task and not context.task.done():
            raise RunError(f"Run '{run_id}' is still finishing.")

        # Registered now so a second resume cannot start a parallel task, and so
        # the run appears in `/runs` again as it finishes.
        self._runs.setdefault(run_id, context)
        context.task = asyncio.create_task(self._execute(context, resume_value=value))
        return context

    # ---------------------------------------------------------------- hydrate
    async def hydrate(self, run_id: str) -> RunContext:
        """Rebuild a run that this process has never seen.

        Everything comes from persistence: the definition from ``Workflow``, the
        inputs and configuration from the ``Run`` row, and the paused state from
        the checkpoints. Nothing is inferred, because a run that comes back with
        different inputs or config would produce a different answer and report
        nothing.

        Raises :class:`RunError` for anything it cannot reconstruct honestly — an
        unknown run, a run that is not waiting on a human, a definition that was
        never saved, or checkpoints that are gone. Refusing is the point: a
        half-built context would fail somewhere deeper and less clearly.
        """
        record = get_run_store().load(run_id)
        if record is None:
            raise RunError(f"Unknown run '{run_id}'.")

        status = record.get("status")
        if status != RunStatus.INTERRUPTED.value:
            # Only a paused run can be picked up. A finished one has no state to
            # continue from, and resuming it would re-run side effects.
            raise RunError(
                f"Run '{run_id}' is {status}, so there is nothing to resume. "
                "Only a run waiting for a human can be rebuilt."
            )

        try:
            # Through the provider, never `app.db.repository` directly: the
            # runtime must stay importable — and *copyable* — without the
            # platform. See `runtime/workflows.py` for why this seam exists.
            graph = load_workflow(record["workflow_id"])
        except WorkflowUnavailable as exc:
            raise RunError(
                f"Run '{run_id}' cannot be rebuilt: its workflow "
                f"'{record['workflow_id']}' was never saved. Runs started from the "
                "canvas without saving reference a draft definition, and a draft has "
                "nothing on disk to restore from. Save the workflow, then run it."
            ) from exc

        settings = get_settings()
        stored_limit = record.get("recursion_limit")
        thread_id = record.get("thread_id") or run_id
        context = RunContext(
            id=run_id,
            workflow_id=record["workflow_id"],
            graph=graph,
            inputs=dict(record.get("inputs") or {}),
            tweaks=dict(record.get("tweaks") or {}),
            # `None` means the row predates the column. Falling back to the
            # configured default is the best available answer, and it says so in
            # the log rather than substituting silently.
            recursion_limit=stored_limit or settings.recursion_limit,
            thread_id=thread_id,
            status=RunStatus.INTERRUPTED,
            checkpointer=get_checkpointer_provider().make(),
            created_at=record.get("created_at") or datetime.now(timezone.utc),
        )
        if stored_limit is None:
            logger.info(
                "Run %s predates the recursion_limit column; using the configured "
                "default of %s.",
                run_id,
                settings.recursion_limit,
            )

        try:
            context.compiled = compile_graph(
                graph,
                checkpointer=context.checkpointer,
                emitter=self._make_emitter(context),
            )
        except CompileError as exc:
            raise RunError(
                f"Run '{run_id}' cannot be rebuilt: its stored definition no longer "
                f"compiles ({exc})."
            ) from exc

        # Read the paused state with LangGraph's own API rather than by hand.
        #
        # The first version of this reached into `CheckpointTuple.pending_writes`
        # and parsed the `Interrupt` objects out of a `(task_id, "__interrupt__",
        # [...])` entry. It worked, and it was the wrong layer: `aget_state`
        # returns a `StateSnapshot` whose `.interrupts` **is** the documented
        # field for exactly this question. Hand-parsing a lower-level structure
        # is not "taking from upstream" — it is reimplementing upstream against
        # something more likely to change.
        #
        # It also answers a second question for free: `next` names the node the
        # run stopped at, so "is there anything to resume?" and "what is it
        # asking?" come from one call.
        snapshot = await self._read_snapshot(context)
        if not snapshot.interrupts:
            raise RunError(
                f"Run '{run_id}' is recorded as awaiting input, but its checkpoints "
                "hold no pending question, so there is no state to continue from. "
                "This happens with CHECKPOINT_STORE=memory, where resume state does "
                "not survive a restart."
            )
        context.interrupt = _interrupt_payload(snapshot.interrupts[0])

        return context

    async def _read_snapshot(self, context: RunContext) -> Any:
        """LangGraph's view of where a run is paused.

        Raises :class:`RunError` when there is nothing to read, because every
        caller is trying to rebuild a run and "no state" means it cannot be.
        """
        config = {"configurable": {"thread_id": context.thread_id}}
        try:
            return await context.compiled.aget_state(config)
        except Exception as exc:  # noqa: BLE001
            raise RunError(
                f"Run '{context.id}' cannot be rebuilt: reading its saved state "
                f"failed ({type(exc).__name__}: {exc})."
            ) from exc

    # ----------------------------------------------------------------- cancel
    async def cancel(self, run_id: str) -> RunContext:
        context = self.require(run_id)

        # A queued run never started, so there is nothing to cancel — it only
        # needs to leave the line. This is the case a semaphore cannot express,
        # and the reason the queue is a real object rather than a counter.
        queue = get_task_queue()
        if context.status is RunStatus.QUEUED and queue.drop(run_id):
            if context.task and not context.task.done():
                try:
                    await context.task
                except Exception:  # noqa: BLE001
                    pass
            return self.require(run_id)

        if context.task and not context.task.done():
            context.task.cancel()
            try:
                await context.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if context.is_active:
            context.status = RunStatus.CANCELLED
            context.finished_at = datetime.now(timezone.utc)
            # `duration_ms` falls back to `time.perf_counter()` when this is
            # unset, so omitting it made a cancelled run report a duration that
            # kept growing forever.
            context.finished_at_monotonic = time.perf_counter()
            context.bus.close()
            self._persist_finish(context)
            self._retire(context)
            await self._forget_checkpoints(context)
        return context

    # ------------------------------------------------------------------- read
    def get(self, run_id: str) -> RunContext | None:
        return self._runs.get(run_id)

    def require(self, run_id: str) -> RunContext:
        context = self._runs.get(run_id)
        if context is None:
            raise RunError(f"Unknown run '{run_id}'. It may have expired.")
        return context

    def list(self, workflow_id: str | None = None, limit: int = 50) -> list[RunContext]:
        runs = [
            run
            for run in self._runs.values()
            if workflow_id is None or run.workflow_id == workflow_id
        ]
        runs.sort(key=lambda run: run.created_at, reverse=True)
        return runs[:limit]

    def retained_ids(self) -> list[str]:
        """Run ids currently held in memory, newest first.

        Public because the retention policy is otherwise invisible: a test (or an
        operator asking "why did this run 404?") needs to see exactly what is
        being kept, and that should not require reaching into a private dict.
        """
        runs = sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)
        return [run.id for run in runs]

    # -------------------------------------------------------------- retention
    def _retire(self, context: RunContext) -> None:
        """Free a finished run's live-only state, then enforce the limits.

        The single place that decides a run is over. Everything that reaches a
        terminal state goes through here, so "released" and "retired" cannot
        drift apart.
        """
        if context.status is RunStatus.INTERRUPTED:
            # A human still owns this run and `resume` needs its checkpointer.
            # Leaving `retired_at` unset is what exempts it from `prune`.
            return
        context.release()
        self.prune()

    def prune(self, *, now: float | None = None) -> list[str]:
        """Drop finished runs from memory. Returns the ids dropped.

        Two independent limits, because either alone is insufficient:

        * **Age** — a run finished ``run_retention_seconds`` ago can no longer be
          the target of an SSE reconnect, which is the only reason a finished run
          is addressable at all.
        * **Count** — age bounds nothing under load. A thousand runs finishing
          inside one retention window would all be young, and all retained.

        Only runs with ``retired_at`` set are candidates, so a run that is
        executing *or* paused for a human is never dropped, whatever the limits
        say. That is the whole safety argument, and it rests on one field.

        Dropping a context loses nothing durable: the ``Run`` and ``RunStep`` rows
        and the transcript on disk are already written and are never touched here.

        ``now`` is injectable so the age rule can be tested without sleeping.
        """
        settings = get_settings()
        clock = time.monotonic() if now is None else now

        # Paired with the timestamp so the count rule can order by age without a
        # second lookup, and so the type checker sees a real float.
        retired = sorted(
            (
                (run.retired_at, run)
                for run in self._runs.values()
                if run.retired_at is not None
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )

        ttl = settings.run_retention_seconds
        doomed = {run.id for retired_at, run in retired if clock - retired_at >= ttl}

        cap = max(settings.max_retained_runs, 0)
        still_fresh = [(t, run) for t, run in retired if run.id not in doomed]
        doomed.update(run.id for _, run in still_fresh[cap:])

        for run_id in doomed:
            self._runs.pop(run_id, None)
        if doomed:
            logger.debug(
                "Pruned %d finished run(s) from memory; %d retained",
                len(doomed),
                len(self._runs),
            )
        return sorted(doomed)

    async def shutdown(self) -> None:
        """Cancel every active run and drain the queue. Called on app shutdown.

        Order is load-bearing: **the queue is drained first**. Cancelling tasks
        first would kill a waiting task inside `queue.acquire`, where the run's
        status has not been recorded yet — so the row it wrote at creation would
        say `queued` forever, in a process that no longer exists. Draining first
        lets each waiting run take the ordinary cancellation path, which sets the
        status and persists it.
        """
        queue = get_task_queue()
        drained = list(queue.snapshot()["waiting"])
        for run_id in drained:
            context = self._runs.get(run_id)
            if queue.drop(run_id) and context is not None and context.task is not None:
                # `drop` makes the waiting task raise `RunCancelledWhileQueued`,
                # which records the outcome. Awaiting it means the drain is
                # finished before the process goes down.
                await asyncio.gather(context.task, return_exceptions=True)

        for context in list(self._runs.values()):
            if context.task and not context.task.done():
                context.task.cancel()

    # ------------------------------------------------------------ persistence
    #
    # These two methods are the security boundary for run history: they are where
    # a credential is removed before a row is written. They build the payload and
    # hand it to the configured store, rather than handing over the live context,
    # so there is exactly one place that redacts and the store has nothing to
    # decide. See `app/runtime/runs.py` for the longer version of that argument.
    def _persist_start(self, context: RunContext) -> None:
        try:
            get_run_store().start(
                {
                    "id": context.id,
                    "workflow_id": context.workflow_id,
                    "status": context.status.value,
                    "inputs": jsonable(redact(context.inputs)),
                    "thread_id": context.thread_id,
                    # Persisted so a run paused here can be rebuilt in a *new
                    # process* with the configuration it started under. Losing
                    # either of these would let a resumed run continue under
                    # defaults and answer differently, with nothing reporting it.
                    "tweaks": jsonable(context.tweaks),
                    "recursion_limit": context.recursion_limit,
                    "created_at": context.created_at,
                }
            )
        except Exception:  # noqa: BLE001
            # Recording history must never abort a run that is otherwise fine —
            # but staying silent about it would hide a broken database forever.
            logger.exception("Failed to record run %s", context.id)

    def _persist_finish(self, context: RunContext) -> None:
        stamped = datetime.now(timezone.utc)
        steps = [
            {
                "run_id": context.id,
                "node_id": record.node_id,
                "node_label": record.node_label,
                "index": record.index,
                "status": record.status,
                # Redacted, like the run-level payloads above and like
                # `api/runs._tracked_step` does for a live run. Without this the
                # same step reads masked while the run is in memory and in the
                # clear once it has been evicted and read back from the store —
                # a credential exposed by waiting a minute.
                "inputs": jsonable(redact(record.inputs)),
                "outputs": jsonable(redact(record.outputs)),
                "error": record.error,
                "started_at": record.started_at,
                "finished_at": stamped,
                "duration_ms": record.duration_ms,
                # Redacted like everything else here, and that is a decision
                # rather than a reflex: a node's log line can name a credential —
                # a node printing its own config, an error quoting a header — and
                # the tests that check redaction cover inputs and outputs, not
                # this. Verified by the same probe that covers those.
                "logs": [redact(line) for line in record.logs],
            }
            for record in _context_step_records(context)
        ]
        try:
            get_run_store().finish(
                {
                    "id": context.id,
                    "workflow_id": context.workflow_id,
                    "status": context.status.value,
                    "inputs": jsonable(redact(context.inputs)),
                    "outputs": jsonable(redact(context.outputs)),
                    "error": context.error,
                    "thread_id": context.thread_id,
                    "created_at": context.created_at,
                    "finished_at": context.finished_at,
                },
                steps,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist run %s", context.id)

        self._write_transcript(context)

    def _write_transcript(self, context: RunContext) -> None:
        """Write the full event log to ``data/runs/<id>.json``.

        Kept out of SQLite on purpose: it is append-heavy, only ever read whole,
        and is far more useful as a file you can open and search.

        Every payload goes through ``redact``, including the per-step ones and the
        event bodies. That is not belt-and-braces: this method previously wrote
        step inputs and outputs **verbatim** while `_persist_finish` redacted the
        same values on its way into SQLite, so the database was clean and the file
        beside it was not. Same defect class as the one fixed in `_persist_finish`,
        in a second place, and it stayed invisible because nothing compared the two
        artefacts.
        """
        import json

        settings = get_settings()
        try:
            settings.ensure_dirs()
            path = settings.runs_dir / f"{context.id}.json"
            payload = {
                "run": {
                    "id": context.id,
                    "workflow_id": context.workflow_id,
                    "status": context.status.value,
                    "inputs": redact(context.inputs),
                    "outputs": redact(context.outputs),
                    "error": context.error,
                    "created_at": context.created_at.isoformat(),
                    "finished_at": (
                        context.finished_at.isoformat() if context.finished_at else None
                    ),
                },
                "steps": [
                    {
                        "node_id": record.node_id,
                        "node_label": record.node_label,
                        "index": record.index,
                        "status": record.status,
                        "inputs": redact(record.inputs),
                        "outputs": redact(record.outputs),
                        "error": record.error,
                        "duration_ms": record.duration_ms,
                    }
                    for record in (
                        context.steps[node_id] for node_id in context.step_order
                    )
                    if record is not None
                ],
                # Event bodies carry a node's outputs too, so they need the same
                # treatment. `data` is the only field that can hold a payload.
                "events": [
                    {**event.model_dump(mode="json"), "data": redact(event.data)}
                    for event in context.bus.history()
                ],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            context.transcript_path = str(path)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write transcript for run %s", context.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _split_result(result: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalise an ``ainvoke`` result across LangGraph versions.

    Older versions return the state dict with a ``__interrupt__`` key; newer ones
    return a ``GraphOutput`` with ``.value`` and ``.interrupts``. Supporting both
    keeps the scaffold working across an upgrade.
    """
    if hasattr(result, "value") and hasattr(result, "interrupts"):
        state = result.value if isinstance(result.value, dict) else {}
        interrupts = [
            _interrupt_payload(item) for item in (result.interrupts or ())
        ]
        return state, [i for i in interrupts if i]

    if isinstance(result, dict):
        raw = result.get("__interrupt__") or []
        interrupts = [_interrupt_payload(item) for item in raw]
        return result, [i for i in interrupts if i]

    return {}, []


def _interrupt_payload(item: Any) -> dict[str, Any] | None:
    """Extract the human-facing payload from an interrupt object."""
    if item is None:
        return None
    value = getattr(item, "value", item)
    if isinstance(value, dict):
        payload = dict(value)
        interrupt_id = getattr(item, "id", None)
        if interrupt_id is not None:
            payload.setdefault("__interrupt_id", interrupt_id)
        return jsonable(payload)
    return jsonable({"value": value})


def _context_step_records(context: RunContext) -> list[StepRecord]:
    """A run's step records in execution order.

    ``step_order`` can name a node that has no record — the name is appended when
    a step starts — so the lookup is filtered rather than trusted. ``api/runs.py``
    walks the same list for live runs, and keeping the rule here is what makes the
    streamed steps and the persisted steps agree on order.
    """
    return [
        record
        for name in context.step_order
        if (record := context.steps.get(name)) is not None
    ]


def _explain(exc: Exception) -> str:
    """Turn an engine exception into something a user can act on."""
    name = type(exc).__name__
    message = str(exc)

    if name == "GraphRecursionError" or "recursion limit" in message.lower():
        return (
            "The workflow exceeded its step limit, which almost always means a loop "
            "has no working exit condition. Check the Loop node's condition, or raise "
            "RECURSION_LIMIT if the workflow legitimately needs more steps."
        )
    if "NodeTimeoutError" in name:
        return f"A node timed out: {message}"
    if name == "InvalidUpdateError":
        return (
            "Two nodes wrote to the same state key in the same step. Give them "
            "distinct output keys, or use a merge reducer."
        )
    return f"{name}: {message}" if message else name


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_manager: RunManager | None = None


def get_run_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager


__all__ = [
    "REPLAY_TAIL",
    "RunContext",
    "RunError",
    "RunManager",
    "StepRecord",
    "get_run_manager",
]
