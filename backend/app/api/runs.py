"""Run endpoints: start, stream, resume, cancel, and single-node debug.

SSE is used for progress rather than WebSockets because the traffic is strictly
one-way (server → client), survives reconnects with a simple `Last-Event-ID`, and
needs no extra protocol to proxy.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.db.repository import WorkflowNotFound, get_workflow
from app.graph.schema import (
    NodeSpec,
    NodeTestRequest,
    RunCreateRequest,
    RunEvent,
    RunStatus,
    WorkflowGraph,
)
from app.nodes import create_node_instance, effective_inputs
from app.runtime.manager import RunError, get_run_manager
from app.runtime.queue import get_task_queue
from app.runtime.runs import get_run_store
from app.runtime.serialize import final_output, redact
from app.security.policy import AccessDenied, enforce_public_graph_policy
from app.settings import get_settings

router = APIRouter(prefix="/runs", tags=["runs"])


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, summary="Start a run")
async def create_run(request: RunCreateRequest, http_request: Request) -> dict[str, Any]:
    """Start a run and return immediately.

    The client then follows ``GET /runs/{id}/events`` for progress. A graph that
    fails to compile is rejected here, with the reason, rather than failing
    silently in the background.
    """
    try:
        graph = get_workflow(request.workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    enforce_public_graph_policy(
        graph,
        get_settings(),
        admin=bool(getattr(http_request.state, "admin", False)),
    )

    manager = get_run_manager()
    try:
        context = await manager.create(request.workflow_id, graph, request)
    except RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await manager.start(context)
    return _run_summary(context)


class AdhocRunRequest(BaseModel):
    """Run a graph supplied in the request body rather than loaded from the store.

    This is what makes "test the workflow I am currently editing" work without
    forcing a save first.
    """

    graph: WorkflowGraph
    inputs: dict[str, Any] = Field(default_factory=dict)
    tweaks: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.post("/adhoc", status_code=status.HTTP_201_CREATED, summary="Run an unsaved graph")
async def create_adhoc_run(request: AdhocRunRequest, http_request: Request) -> dict[str, Any]:
    enforce_public_graph_policy(
        request.graph,
        get_settings(),
        admin=bool(getattr(http_request.state, "admin", False)),
    )
    manager = get_run_manager()
    run_request = RunCreateRequest(
        workflow_id=request.graph.id,
        inputs=request.inputs,
        tweaks=request.tweaks,
    )
    try:
        context = await manager.create(request.graph.id, request.graph, run_request)
    except RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await manager.start(context)
    return _run_summary(context)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("", summary="List runs currently held in memory")
def list_runs(
    workflow_id: str | None = None, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    """Only runs still in memory, which is necessarily a recent subset.

    Finished runs are evicted after a retention window (``runtime.manager``), so
    this list shortens over time. ``/runs/history`` is the complete record.

    ``queue`` reports the concurrency gate: the limit, what is executing, and the
    waiting ids in order. Sent with the list rather than from its own endpoint
    because a client drawing "3 running, 2 waiting" needs both answers to be from
    the same instant, and two requests would not be.
    """
    return {
        "runs": [_run_summary(run) for run in get_run_manager().list(workflow_id, limit)],
        "queue": get_task_queue().snapshot(),
    }


@router.get("/history", summary="List persisted runs from the database")
def run_history(
    workflow_id: str | None = None, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    """Survives a restart, unlike the in-memory run list above."""
    return {"runs": get_run_store().history(workflow_id, limit)}


@router.get("/{run_id}", summary="Get a run's current state")
async def get_run(run_id: str) -> dict[str, Any]:
    """Live state while the run is in memory, otherwise its persisted row.

    Finished runs are evicted from memory after a retention window, so a plain
    ``get`` would 404 a run that plainly did happen. Answering from the database
    instead keeps the memory bound from being visible as a bug. ``/runs/history``
    and ``/runs/{id}/steps`` already worked this way; this removes the last
    endpoint that did not.

    ``async`` because a **paused** run needs one more read than a finished one:
    the pending question lives in the checkpoint, not in the row. That read has to
    happen on the application's event loop — the checkpointer's connection is
    bound to it, so doing it from a sync route would mean a second loop and a
    broken connection.
    """
    context = get_run_manager().get(run_id)
    if context is None:
        return await _persisted_summary_with_interrupt(run_id, include_steps=True)
    return _run_summary(context, include_steps=True)


async def _persisted_summary_with_interrupt(
    run_id: str, *, include_steps: bool = False
) -> dict[str, Any]:
    """A persisted summary, with the pending question filled in from the checkpoints.

    ``_persisted_summary`` necessarily reports ``interrupt: None`` — it reads a
    row, and the payload is not in the row. For a **finished** run that is honest
    and correct. For a paused one it is misleading in a specific way: the UI
    renders an empty review box for a question that has a message and fields, so
    the reviewer sees nothing to answer.

    The rebuild is delegated to :meth:`RunManager.hydrate` rather than repeated
    here. That method already knows how to turn a row plus a checkpoint into a
    run, and a second, thinner copy of that knowledge is exactly how the two paths
    drift — the same failure this project has already hit twice with duplicated
    read paths.

    A run that cannot be rebuilt keeps ``interrupt: None``, which is what its row
    means. The status is left as it is rather than quietly corrected: a run
    recorded as paused with nothing to resume from is a real state, and hiding it
    would remove the only signal that something is wrong.
    """
    payload = _persisted_summary(run_id, include_steps=include_steps)
    if payload.get("status") != RunStatus.INTERRUPTED.value:
        return payload

    from app.runtime.manager import RunError, get_run_manager

    try:
        context = await get_run_manager().hydrate(run_id)
    except RunError:
        return payload
    if context.interrupt:
        payload["interrupt"] = context.interrupt
    return payload


@router.get("/{run_id}/steps", summary="Per-node step results for a run")
def get_run_steps(run_id: str) -> dict[str, Any]:
    context = get_run_manager().get(run_id)
    if context is not None:
        return {"steps": [_tracked_step(record) for record in _context_steps(context)]}

    return {"steps": get_run_store().steps(run_id)}


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------


#: The SSE frame separator, pinned rather than defaulted, and it has to be set in
#: *both* places below — the response AND every event.
#:
#: `sse-starlette` defaults to CRLF, and `EventSourceResponse(sep=...)` does not
#: reach events handed to it already built, so a response-level `sep` alone
#: silently produces CRLF. That matters because the wire format is a contract
#: with two hand-written consumers — the canvas's frame parser
#: (`frontend/src/api/client.ts` splits frames on `"\n\n"`) and
#: `scripts/smoke.py` — and a CRLF stream contains no `"\n\n"` at all. The
#: failure mode is the worst kind: the stream connects, events arrive, and
#: nothing ever renders. The response-level value still matters, because the
#: 15-second keep-alive ping is built with `sep=self.sep`.
_SSE_SEP = "\n"


@router.get("/{run_id}/events", summary="Stream run progress as Server-Sent Events")
async def stream_events(
    run_id: str,
    after: int = Query(default=0, ge=0, description="Last sequence number already seen."),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """SSE stream of :class:`RunEvent`.

    ``after`` (or the standard ``Last-Event-ID`` header) makes reconnection
    lossless: the bus replays everything the client missed before going live.

    Two honest limits, both reported rather than papered over:

    * A **finished** run keeps only the end of its history (see ``REPLAY_TAIL``),
      so a device that was away for the whole run gets a partial replay. The
      ``X-Replay-From`` response header carries the oldest sequence number still
      available, which is how a caller can tell a complete replay from a tail.
    * An **evicted** run has no bus at all, which is ``410 Gone`` — the run is
      real and durable, it is only the live stream that is unavailable.
    """
    context = get_run_manager().get(run_id)
    if context is None:
        # 410 rather than 404: the run existed, and telling these two apart is
        # the difference between "you have a bug" and "read it from history".
        # Answered from the database, because memory alone cannot tell an evicted
        # run from a run id that was never real.
        if not _is_known_run(run_id):
            raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'.")
        raise HTTPException(
            status_code=410,
            detail=(
                f"Run '{run_id}' is no longer in memory, so its event stream "
                "cannot be replayed. Read /runs/history, "
                f"/runs/{run_id}/steps, or the transcript under data/runs/."
            ),
        )

    # Honour the SSE reconnect header when the client does not pass `after`.
    if after == 0 and last_event_id:
        try:
            after = int(last_event_id)
        except ValueError:
            after = 0

    async def generator() -> AsyncIterator[ServerSentEvent]:
        async for event in context.bus.subscribe(after=after):
            yield ServerSentEvent(
                data=json.dumps(
                    event.model_dump(mode="json"), ensure_ascii=False, default=str
                ),
                event=event.type.value,
                id=str(event.seq),
                sep=_SSE_SEP,
            )

    return EventSourceResponse(
        generator(),
        sep=_SSE_SEP,
        headers={
            # Overrides the library's `no-store`. `no-transform` is the stronger
            # one for this case: it is what stops a proxy from re-encoding the
            # stream. `Connection` and `X-Accel-Buffering` are left to the
            # library, which sets the same values.
            "Cache-Control": "no-cache, no-transform",
            # The oldest sequence number this stream can still produce. If it is
            # greater than `after + 1` the replay has a hole, because the run
            # finished and its early history was released. Sent as a header so the
            # reassuring word "lossless" above is never claimed falsely.
            "X-Replay-From": str(context.bus.earliest_seq),
        },
    )


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


@router.post("/{run_id}/resume", summary="Resume an interrupted run")
async def resume_run(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    manager = get_run_manager()
    try:
        context = await manager.resume(run_id, payload)
    except RunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_summary(context)


@router.post("/{run_id}/cancel", summary="Cancel a running run")
async def cancel_run(run_id: str) -> dict[str, Any]:
    manager = get_run_manager()
    try:
        context = await manager.cancel(run_id)
    except RunError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _run_summary(context)


# ---------------------------------------------------------------------------
# Single-node debugging
# ---------------------------------------------------------------------------


@router.post("/node-test", summary="Run one node in isolation")
async def test_node(request: NodeTestRequest, http_request: Request) -> dict[str, Any]:
    """Execute a single node with hand-supplied upstream input.

    The most-used debugging affordance in tools of this kind: you fix a prompt by
    running *that one node*, not the whole workflow. Upstream values are passed in
    directly, so no dependencies are executed.

    The node comes from the request when the canvas supplies it, and from the
    stored workflow otherwise. Both paths are load-bearing: the canvas has to be
    able to test a node it has not saved yet (a node drawn seconds ago is not in
    the store, and the stored config is not the one under the cursor), while a
    caller with nothing but an id — a script, a test — still gets the saved
    definition.
    """
    node: NodeSpec | None = None
    if request.node:
        try:
            node = NodeSpec.model_validate(request.node)
        except Exception as exc:  # noqa: BLE001 - the shape error *is* the answer
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        try:
            graph = get_workflow(request.workflow_id)
        except WorkflowNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        node = graph.node(request.node_id)

    if node is None:
        raise HTTPException(status_code=404, detail=f"No node '{request.node_id}'.")

    settings = get_settings()
    if settings.public_demo and node.type == "code" and not getattr(
        http_request.state, "admin", False
    ):
        raise AccessDenied(
            "demo_disabled",
            detail="Code nodes are display-only in the public demo.",
        )
    instance = create_node_instance(node)

    # Present every declared input handle, so a node that reads an unconnected
    # handle still resolves it to None instead of raising KeyError.
    inputs: dict[str, Any] = {port.id: None for port in effective_inputs(node)}
    inputs.update(request.inputs or {})

    merged_config = {**instance.defaults_for(instance.spec), **(node.config or {})}
    merged_config.update(request.config_overrides or {})

    from app.nodes.base import NodeContext

    logs: list[dict[str, str]] = []

    async def collect_log(message: str, level: str = "info") -> None:
        logs.append({"message": message, "level": level})

    async def ignore(_payload: dict[str, Any]) -> None:
        return None

    ctx = NodeContext(
        node=node,
        config=merged_config,
        inputs=inputs,
        state={"vars": {}, "inputs": request.inputs or {}},
        settings=settings,
        emit=ignore,
        log=collect_log,
    )

    try:
        result = await instance.run(ctx)
    except Exception as exc:  # noqa: BLE001 - the error *is* the response
        # The local development surface wants the message; a gate visitor does
        # not get exception text, which can carry paths or configuration.
        error = (
            "The node failed. The full message is in the server log."
            if settings.public_access
            else f"{type(exc).__name__}: {exc}"
        )
        return {
            "ok": False,
            "error": error,
            "logs": logs,
        }

    return {
        "ok": True,
        "outputs": result,
        "config": redact(merged_config),
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_known_run(run_id: str) -> bool:
    """Has this run id ever existed?

    Asked only when the in-memory map has nothing, so the extra query is paid on
    the terminal path — once per finished run — and never while streaming.
    """
    return get_run_store().exists(run_id)


def _context_steps(context: Any) -> list[Any]:
    """A live run's step records, in execution order."""
    return [context.steps[name] for name in context.step_order if name in context.steps]


def _tracked_step(record: Any) -> dict[str, Any]:
    """Shape an in-memory step record for the wire.

    Redacted here because these snapshots are the raw node inputs and outputs,
    which can hold a credential the transcript is not allowed to carry.
    """
    return {
        "node_id": record.node_id,
        "node_label": record.node_label,
        "index": record.index,
        "status": record.status,
        "inputs": redact(record.inputs),
        "outputs": redact(record.outputs),
        "error": record.error,
        "duration_ms": record.duration_ms,
        # Same treatment as the other payloads here, and for the same reason: a
        # log line can quote a credential. This is the live shape, so without it
        # a client would see logs while running and lose them on reload — the
        # asymmetry the store-side redaction comment warns about, one field over.
        "logs": [redact(line) for line in (record.logs or [])],
    }


def _run_summary(context: Any, *, include_steps: bool = False) -> dict[str, Any]:
    # Redacted once and reused. `final_output` reads this same masked dict rather
    # than a fresh redaction, so the pinned result cannot become a second read
    # path with a masking rule of its own.
    outputs = redact(context.outputs)
    payload: dict[str, Any] = {
        "id": context.id,
        "workflow_id": context.workflow_id,
        "status": context.status.value,
        "inputs": redact(context.inputs),
        "outputs": outputs,
        "final_output": final_output(outputs),
        "error": context.error,
        "interrupt": getattr(context, "interrupt", None),
        "created_at": context.created_at,
        "finished_at": context.finished_at,
        "duration_ms": context.duration_ms,
        # What the run spent, and against which limits. Present even when
        # nothing is limited: the figure is how a deployment decides what to set
        # the limits to, so hiding it until something is enforced would hide it
        # exactly when it is useful.
        "usage": context.budget.usage(),
        "last_seq": context.bus.last_seq,
    }
    if include_steps:
        payload["steps"] = [_tracked_step(record) for record in _context_steps(context)]
    return payload


def _persisted_summary(run_id: str, *, include_steps: bool = False) -> dict[str, Any]:
    """Rebuild a run summary from the store, for a run no longer in memory.

    The key set is identical to :func:`_run_summary` so a client sees one shape,
    never two. Two values are necessarily different, and both stay honest rather
    than invented:

    * ``last_seq`` is 0 — the event bus is gone, so there is nothing to replay
      from; the episodes are in the transcript file.
    * ``interrupt`` is None — a run paused for a human is never evicted, so an
      evicted run cannot be awaiting input.

    ``duration_ms`` is not in that list: the store derives it from the same two
    timestamps the live summary derives it from, so the two agree by construction
    rather than by two copies of a rounding rule.

    ``usage`` is None for the same reason as ``interrupt``: a persisted row does
    not record spend, and a run that is still in memory is the only one with a
    budget object. Reporting zeros would claim the run used nothing.
    """
    store = get_run_store()
    payload = store.summary(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'.")

    payload.update(interrupt=None, last_seq=0, usage=None)
    if include_steps:
        payload["steps"] = store.steps(run_id)
    return payload
