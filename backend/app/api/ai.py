"""AI endpoints: describe a node, generate a graph, repair one, convert code."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.ai_buildtime.copilot import CopilotError, run_copilot
from app.ai_buildtime.gen_graph import GraphBuildError, generate_graph
from app.ai_buildtime.gen_node import (
    AIError,
    AINotConfigured,
    code_to_node,
    fill_node,
    node_to_code,
)
from app.runtime.ai.provider import is_mock_configured
from app.ai_buildtime.repair import repair_graph
from app.db.repository import WorkflowNotFound, get_workflow
from app.graph.schema import NodeSpec, WorkflowGraph, normalize_language
from app.security.policy import message as policy_message, require_admin
from app.security.quotas import QuotaExceeded
from app.settings import get_settings

router = APIRouter(prefix="/ai", tags=["ai"])


def _ai_failure(exc: Exception) -> str:
    """Provider errors can name a base URL or echo a request; hide them publicly."""
    if isinstance(exc, QuotaExceeded):
        # A quota stop is a policy answer, not a provider failure. Re-raise so
        # the application's AccessDenied handler returns 429 with its stable code
        # instead of turning it into a 502.
        raise exc
    if get_settings().public_access:
        return "The AI request failed. The full message is in the server log."
    return f"AI request failed: {exc}"


class FillNodeRequest(BaseModel):
    """Ask the AI to configure one node from its description."""

    graph: WorkflowGraph
    node_id: str
    #: Optional nudge, e.g. "make it shorter" or "answer in Chinese".
    instruction: str = ""
    #: Language for the one sentence this endpoint authors itself. Present on all
    #: three AI endpoints so a client does not have to remember which of them
    #: localises its own text.
    lang: str = "en"


class GenerateGraphRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    #: When supplied, the generated graph extends this canvas instead of starting fresh.
    graph: WorkflowGraph | None = None
    #: Language for the validation report that comes back with the draft.
    lang: str = "en"
    #: Trial-run the draft before returning it. `None` defers to the server's
    #: AI_SELF_TEST setting so a client that does not know about the feature gets
    #: the deployment's policy rather than silently opting in or out.
    self_test: bool | None = None


class CopilotSelection(BaseModel):
    """The region the user pointed at. Empty means "the whole canvas"."""

    nodes: list[str] = Field(default_factory=list)
    edges: list[str] = Field(default_factory=list)


class CopilotRequest(BaseModel):
    """One turn with the editing copilot.

    ``approval`` is the payload the server sent on the ``approval`` event. A
    second call carrying it executes the plan the user actually saw; without it,
    ``mode="confirm"`` stops after planning.
    """

    message: str = Field(min_length=1, max_length=8000)
    graph: WorkflowGraph
    mode: Literal["auto", "confirm"] = "confirm"
    selection: CopilotSelection = Field(default_factory=CopilotSelection)
    sample_input: dict[str, Any] | None = None
    expected_output: str = ""
    last_output: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    #: Earlier turns (`{"role": "user"|"assistant", "content": ...}`). Bounded
    #: again server-side; the client only needs to send what it kept.
    history: list[dict[str, str]] = Field(default_factory=list)
    lang: str = "en"
    self_test: bool | None = None
    #: Ask for a deeper decision rationale, shown in the collapsible block.
    thinking: bool = False
    #: Inline data URLs (PNG/JPEG/GIF/WebP). DeepSeek accepts images in user
    #: messages only; they are attached to the insight/plan/edit messages.
    images: list[str] = Field(default_factory=list, max_length=4)


#: Data URLs the provider accepts. The declared prefix is checked here; DeepSeek
#: checks the actual bytes and rejects a mismatch.
_IMAGE_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/jpg;base64,",
    "data:image/gif;base64,",
    "data:image/webp;base64,",
)
_MAX_IMAGE_CHARS = 8_000_000


def _validate_images(images: list[str]) -> None:
    """Reject non-image or oversized attachments before they reach the model."""
    for data in images:
        if not data.startswith(_IMAGE_PREFIXES):
            raise HTTPException(
                status_code=422,
                detail="Images must be PNG, JPEG, GIF or WebP data URLs.",
            )
        if len(data) > _MAX_IMAGE_CHARS:
            raise HTTPException(
                status_code=422,
                detail="Each image must be at most about 6 MB.",
            )


class NodeToCodeRequest(BaseModel):
    graph: WorkflowGraph
    node_id: str


class CodeToNodeRequest(BaseModel):
    graph: WorkflowGraph
    node_id: str
    code: str
    target_type: str


class RepairGraphRequest(BaseModel):
    """Ask the AI to clear the validator's complaints about a graph."""

    graph: WorkflowGraph
    #: "error" (default) chases only blocking problems; "all" also chases warnings.
    #: Constrained to a literal so a typo fails at the schema boundary rather than
    #: silently selecting the fallback branch and reporting "nothing to fix".
    severity: Literal["error", "all"] = "error"
    #: Optional nudge, e.g. "prefer a Loop node over a bounded Code node".
    instruction: str = ""
    #: Language for server-authored text and the model's new node descriptions.
    lang: str = "en"
    #: Step ceiling for the repair loop; falls back to the configured default.
    max_iterations: int | None = Field(default=None, ge=1, le=50)


@router.get("/status", summary="Whether the AI features are usable")
def status() -> dict[str, Any]:
    """Lets the UI hide AI affordances instead of offering buttons that fail."""
    settings = get_settings()
    mock = is_mock_configured(settings)
    return {
        "enabled": settings.ai_enabled,
        "configured": not mock,
        "mock": mock,
        "model": settings.llm_model,
        "base_url": "" if settings.public_access else settings.llm_base_url,
        "reason": (
            None
            if settings.ai_enabled and not mock
            else (
                "AI features are disabled (AI_ENABLED=false)."
                if not settings.ai_enabled
                else "No LLM API key is set, so AI features are unavailable. "
                "Add LLM_API_KEY to .env and restart the backend."
            )
        ),
    }


@router.post("/fill-node", summary="Suggest configuration for a node")
async def fill(request: FillNodeRequest) -> dict[str, Any]:
    """Turn a node's plain-language description into typed config values.

    Returns a suggestion with a diff. The client shows it for approval — this
    endpoint never mutates the user's graph.
    """
    node = request.graph.node(request.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node '{request.node_id}'.")
    if not (node.description or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "This node has no description yet. Write what it should do, then ask "
                "the AI to fill it in."
            ),
        )

    try:
        return await fill_node(
            request.graph,
            node,
            extra_instruction=request.instruction,
            lang=normalize_language(request.lang),
        )
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_ai_failure(exc)) from exc


@router.post("/generate-graph", summary="Draft a workflow from a description")
async def generate(request: GenerateGraphRequest) -> dict[str, Any]:
    """Build a workflow from natural language and return it as a preview.

    The response is a *draft*: the client renders it as an overlay the user can
    accept or discard, so an unhelpful generation never destroys their work.
    """
    try:
        return await generate_graph(
            request.prompt,
            existing=request.graph,
            lang=normalize_language(request.lang),
            self_test=request.self_test,
        )
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GraphBuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_ai_failure(exc)) from exc


@router.post(
    "/generate-graph/stream",
    summary="Draft a workflow, streaming each change as it happens",
)
async def generate_stream(request: GenerateGraphRequest) -> StreamingResponse:
    """Same contract as ``/generate-graph``, delivered as Server-Sent Events.

    The generation is otherwise identical. Streaming exists so the client can
    draw the draft while it is being built instead of staring at a spinner for a
    minute: every mutating tool call produces a `graph` event carrying the whole
    snapshot, rather than an instruction the client would have to replay. One
    source of truth for ids and layout, and a client that reconnects misses
    nothing important because the next event is complete.

    The final ``done`` event carries exactly the JSON the non-streaming endpoint
    returns, so the dialog has one code path for the result.

    A dropped connection cancels the generation. Nothing has been applied
    server-side, so the only thing wasted would be tokens — better spent
    elsewhere than on a dialog nobody is looking at.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_event(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def work() -> None:
        try:
            result = await generate_graph(
                request.prompt,
                existing=request.graph,
                lang=normalize_language(request.lang),
                self_test=request.self_test,
                on_event=on_event,
            )
            await queue.put({"type": "done", "result": result})
        except AINotConfigured as exc:
            await queue.put({"type": "error", "code": "not_configured", "message": str(exc)})
        except GraphBuildError as exc:
            await queue.put({"type": "error", "code": "build_failed", "message": str(exc)})
        except QuotaExceeded:
            await queue.put(
                {
                    "type": "error",
                    "code": "quota_exceeded",
                    "message": policy_message(
                        "quota_exceeded", normalize_language(request.lang)
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - the stream itself must not hang
            await queue.put(
                {"type": "error", "code": "exception", "message": _ai_failure(exc)}
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(work())

    async def frames():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # A proxy that buffers until the first byte would turn a
                    # two-minute self-test into a dialog that looks frozen.
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/copilot/stream",
    summary="Understand, plan and edit the workflow, streaming every stage",
)
async def copilot_stream(request: CopilotRequest) -> StreamingResponse:
    """Edit an existing workflow with a scoped, verifiable copilot turn.

    The stream is the UI's stage machine: ``stage`` events drive the layered
    indicator above the canvas, ``graph`` events are complete snapshots so the
    canvas never replays edits, and ``approval`` ends the confirm-mode planning
    pass. The final ``done`` event carries the same payload a non-streaming call
    would, including the business brief and the change plan.
    """
    _validate_images(request.images)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_event(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def work() -> None:
        try:
            result = await run_copilot(
                request.message,
                request.graph,
                mode=request.mode,
                selection_nodes=request.selection.nodes,
                selection_edges=request.selection.edges,
                sample_input=request.sample_input,
                expected_output=request.expected_output,
                last_output=request.last_output,
                approval=request.approval,
                history=request.history,
                lang=normalize_language(request.lang),
                self_test=request.self_test,
                thinking=request.thinking,
                images=request.images,
                on_event=on_event,
            )
            await queue.put({"type": "done", "result": result})
        except AINotConfigured as exc:
            await queue.put({"type": "error", "code": "not_configured", "message": str(exc)})
        except (GraphBuildError, CopilotError) as exc:
            await queue.put({"type": "error", "code": "build_failed", "message": str(exc)})
        except QuotaExceeded:
            await queue.put(
                {
                    "type": "error",
                    "code": "quota_exceeded",
                    "message": policy_message(
                        "quota_exceeded", normalize_language(request.lang)
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - the stream itself must not hang
            await queue.put(
                {"type": "error", "code": "exception", "message": _ai_failure(exc)}
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(work())

    async def frames():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/repair-graph", summary="Fix the validator's complaints about a workflow")
async def repair(request: RepairGraphRequest) -> dict[str, Any]:
    """Clear a graph's validation problems and return the result as a preview.

    Same contract as ``/generate-graph``: nothing is applied server-side, and the
    response is a *draft* the client previews before accepting. That matters more
    here than anywhere else in this module, because repairing is the one AI
    feature whose edits are judged against a machine check — and "delete the
    offending nodes" satisfies the check while destroying the user's work. The
    payload therefore carries a ``diff`` and a ``regressed`` flag alongside the
    graph, so the UI can show exactly what would be removed.

    Returns 200 with ``changed: false`` when the graph was already valid; that is
    a legitimate outcome, not an error.
    """
    try:
        return await repair_graph(
            request.graph,
            severity=request.severity,
            lang=normalize_language(request.lang),
            instruction=request.instruction,
            max_iterations=request.max_iterations,
        )
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GraphBuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_ai_failure(exc)) from exc


@router.post("/node-to-code", summary="Convert a node into equivalent Python")
async def to_code(
    request: NodeToCodeRequest, _admin: None = Depends(require_admin)
) -> dict[str, Any]:
    node = request.graph.node(request.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node '{request.node_id}'.")

    try:
        return await node_to_code(request.graph, node)
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_ai_failure(exc)) from exc


@router.post("/code-to-node", summary="Convert Python into configuration for a node type")
async def to_node(
    request: CodeToNodeRequest, _admin: None = Depends(require_admin)
) -> dict[str, Any]:
    node = request.graph.node(request.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node '{request.node_id}'.")

    try:
        return await code_to_node(
            request.graph,
            node,
            code=request.code,
            target_type=request.target_type,
        )
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_ai_failure(exc)) from exc


# ---------------------------------------------------------------------------
# Saved-workflow convenience wrappers
# ---------------------------------------------------------------------------


class SavedFillRequest(BaseModel):
    """Fill a node in a *saved* workflow, by id rather than by payload."""

    workflow_id: str
    node_id: str
    instruction: str = ""
    lang: str = "en"


@router.post("/workflows/fill-node", summary="Fill a node in a saved workflow")
async def fill_saved(request: SavedFillRequest) -> dict[str, Any]:
    try:
        graph = get_workflow(request.workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    node = graph.node(request.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node '{request.node_id}'.")

    try:
        return await fill_node(
            graph,
            node,
            extra_instruction=request.instruction,
            lang=normalize_language(request.lang),
        )
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class RenameRequest(BaseModel):
    graph: WorkflowGraph


@router.post("/suggest-name", summary="Suggest a name for a workflow")
async def suggest_name(request: RenameRequest) -> dict[str, str]:
    """Small convenience: name a workflow from its nodes' descriptions."""
    descriptions = [
        f"- {node.label or node.id} ({node.type}): {node.description}"
        for node in request.graph.executable_nodes()
        if node.description
    ]
    if not descriptions:
        raise HTTPException(
            status_code=422,
            detail="Add descriptions to some nodes first, then I can suggest a name.",
        )

    from app.ai_buildtime.gen_node import _complete

    try:
        content = await _complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Suggest a short workflow name (3-6 words) from the step "
                        "descriptions. Reply with the name only, no quotes or "
                        "punctuation."
                    ),
                },
                {"role": "user", "content": "\n".join(descriptions)},
            ]
        )
    except AINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"name": content.strip().strip('"').strip()[:80]}


__all__ = ["router"]
