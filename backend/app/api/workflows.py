"""Workflow CRUD, validation, and import/export."""

from __future__ import annotations

import pathlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.db.repository import (
    WorkflowNotFound,
    WorkflowVersionNotFound,
    count_workflows,
    delete_workflow,
    get_workflow,
    get_workflow_version,
    import_workflow,
    list_workflow_versions,
    list_workflows,
    save_workflow,
    workflow_exists,
)
from app.graph.layout import layout_graph
from app.graph.schema import WorkflowGraph, localize
from app.graph.validate import validate_graph
from app.nodes import effective_inputs, effective_outputs
from app.security.policy import (
    AccessDenied,
    enforce_public_graph_policy,
    require_admin,
)
from app.settings import get_settings

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _public_write_check(request: Request, graph: WorkflowGraph, *, creating: bool) -> None:
    """The public demo's write boundary.

    Validation warns about Code nodes so a workflow can be opened and read. This
    is where the operation is refused, because saving or running is what turns
    the warning into a consequence.
    """
    settings = get_settings()
    if not settings.public_demo:
        return
    admin = bool(getattr(request.state, "admin", False))
    enforce_public_graph_policy(graph, settings, admin=admin)
    if admin:
        return
    if settings.max_workflows:
        if creating and not workflow_exists(graph.id) and count_workflows() >= settings.max_workflows:
            raise AccessDenied(
                "demo_disabled",
                status_code=429,
                detail=(
                    f"The public demo holds at most {settings.max_workflows} "
                    "workflows. Delete one before creating another."
                ),
            )


LangQuery = Query(
    default="en",
    description="Language for human-readable text (en, zh). Identifiers never change.",
)


class WorkflowSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    version: int = 1
    created_at: Any = None
    updated_at: Any = None
    node_count: int = 0


@router.get("", summary="List saved workflows")
def index() -> dict[str, Any]:
    return {"workflows": list_workflows()}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a workflow")
def create(graph: WorkflowGraph, request: Request) -> WorkflowGraph:
    _public_write_check(request, graph, creating=True)
    if get_settings().public_demo and workflow_exists(graph.id):
        # `save_workflow` is an upsert. Without this check a POST could silently
        # replace the seeded showcase workflow; the canvas uses PUT for updates.
        raise HTTPException(
            status_code=409,
            detail=f"Workflow '{graph.id}' already exists; use PUT to update it.",
        )
    _check_tool_sources(graph)
    try:
        return save_workflow(graph)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{workflow_id}", summary="Fetch one workflow")
def show(workflow_id: str) -> WorkflowGraph:
    try:
        return get_workflow(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{workflow_id}/versions", summary="List a workflow's saved versions")
def versions(workflow_id: str) -> dict[str, Any]:
    """Every version of this definition, newest first.

    Summaries, not payloads — see the repository function. A workflow with fifty
    versions should not return fifty whole graphs to render a list of dates.
    """
    try:
        items = list_workflow_versions(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "items": [
            {**item, "created_at": item["created_at"].isoformat()} for item in items
        ]
    }


@router.get("/{workflow_id}/versions/{version}", summary="Fetch one saved version")
def show_version(workflow_id: str, version: int) -> WorkflowGraph:
    """A historical definition, exactly as it was saved.

    This is what makes a run from months ago reproducible: the caller gets the
    definition that produced it, not the one on the canvas now. It raises rather
    than falling back to the current version — answering with a *different*
    definition than the one asked for would be worse than failing.
    """
    try:
        return get_workflow_version(workflow_id, version)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowVersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # A stored graph that no longer validates against the current schema.
        # 409 rather than 500: the request was fine and the data is real, they just
        # disagree — and the version exists, so deleting the endpoint would hide it.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/{workflow_id}", summary="Save a workflow")
def update(workflow_id: str, graph: WorkflowGraph, request: Request) -> WorkflowGraph:
    # The path id wins, so a client cannot accidentally write to another record.
    graph.id = workflow_id
    _public_write_check(request, graph, creating=not workflow_exists(workflow_id))
    _check_tool_sources(graph)
    try:
        return save_workflow(graph)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class SaveWithChangeRequest(BaseModel):
    """Save the canvas together with the copilot record that produced it."""

    graph: WorkflowGraph
    change: dict[str, Any] = Field(default_factory=dict)


@router.put(
    "/{workflow_id}/ai",
    summary="Save a workflow and record the copilot change on its version",
)
def update_with_change(
    workflow_id: str, payload: SaveWithChangeRequest, request: Request
) -> WorkflowGraph:
    """Same as ``PUT /{id}``, plus an audit record on the new version.

    The record lives on the version row, so restoring that version still gives
    the exact definition while the version list can explain why it exists.
    """
    payload.graph.id = workflow_id
    _public_write_check(
        request, payload.graph, creating=not workflow_exists(workflow_id)
    )
    _check_tool_sources(payload.graph)
    try:
        return save_workflow(
            payload.graph,
            meta={"ai_change": payload.change} if payload.change else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a workflow")
def destroy(workflow_id: str, _admin: None = Depends(require_admin)) -> None:
    try:
        delete_workflow(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ToolSourceRequest(BaseModel):
    """A tool module as written in the canvas."""

    name: str
    source: str


def _check_tool_sources(graph: WorkflowGraph) -> None:
    """Refuse a save whose tool modules could never be exported.

    Only shape is checked here, never behaviour: importing a candidate module
    while saving a workflow would run arbitrary code on every keystroke-triggered
    autosave. Behaviour is what `/tools/check` is for, and it is called on
    demand. This split is the reason the save stays fast.

    Called on create as well as update. A workflow created through the API with a
    bad module name would otherwise be storable but not exportable, which is the
    kind of failure that only shows up much later.
    """
    from app.tools.authoring import ToolSourceError, validate_module_name, validate_source

    for name, source in graph.tools.items():
        try:
            validate_module_name(name)
            validate_source(source)
        except ToolSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tools/check", summary="Check a tool module written in the canvas")
def check_tool(
    request: ToolSourceRequest, _admin: None = Depends(require_admin)
) -> dict[str, Any]:
    """Compile and import a candidate tool module in a throwaway process.

    Returns a report rather than raising for a bad module: a module that does not
    work is the expected input here, not an exceptional condition. `tools` names
    what it registered, which is what the editor shows instead of asking the user
    to take it on trust.

    Runnable before the workflow is saved, so the editor's Check button does not
    require saving half-finished work.

    The check is a pure function of the source — it deliberately does not read the
    workflow, so a tool can be checked while the rest of the graph is invalid.
    """
    from app.tools.authoring import ToolSourceError, check_module

    try:
        return check_module(request.name, request.source)
    except ToolSourceError as exc:
        return {"ok": False, "error": str(exc), "tools": [], "warnings": []}


@router.get(
    "/{workflow_id}/tools",
    summary="List a workflow's own tool modules",
)
def list_tools(workflow_id: str) -> dict[str, Any]:
    """The editor's tool list, read from the saved workflow.

    Read-only: modules are written by saving the workflow itself. A separate
    write endpoint would let the graph in the editor and the graph on disk
    disagree, and the exporter reads the latter.
    """
    if get_settings().public_demo:
        # Saved modules are not runnable in the demo, so returning their source
        # would only invite a visitor to edit something that can never execute.
        return {"tools": []}
    try:
        graph = get_workflow(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "tools": [
            {"name": name, "source": source, "bytes": len(source.encode("utf-8"))}
            for name, source in sorted(graph.tools.items())
        ]
    }


@router.post("/layout", summary="Re-arrange a graph's node positions")
def layout(
    graph: WorkflowGraph,
    per_band: int = Query(
        6,
        ge=0,
        le=64,
        description="Columns per band; 0 lays the graph out as one straight line.",
    ),
) -> dict[str, Any]:
    """Return the same graph with tidy layered positions.

    The canvas asks for this when the user clicks "tidy layout". Nothing is
    saved: the response is an ordinary graph the client applies (and can undo)
    like any other edit, so a layout change gets the same history treatment as
    moving a node by hand.
    """
    return {"graph": layout_graph(graph, per_band=per_band).model_dump(mode="json")}


@router.post("/validate", summary="Validate a graph without saving it")
def validate(graph: WorkflowGraph, lang: str = LangQuery) -> dict[str, Any]:
    """Used by the canvas to show live problems in the Inspector.

    Returns a report rather than raising, because a work-in-progress graph is
    expected to be invalid most of the time.

    The report is localized on the way out — ``message`` carries the user-facing
    sentence while ``code`` stays stable across languages, so nothing downstream
    has to map codes to text itself.
    """
    report = validate_graph(graph)
    return localize(report.model_dump(mode="json"), lang)


@router.post("/{workflow_id}/export", summary="Write the Json export to disk")
def export(workflow_id: str, _admin: None = Depends(require_admin)) -> dict[str, str]:
    from app.db.repository import export_workflow

    try:
        graph = get_workflow(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"path": export_workflow(graph)}


@router.post(
    "/{workflow_id}/export-agent",
    summary="Export a runnable agent package",
)
def export_agent(
    workflow_id: str, _admin: None = Depends(require_admin)
) -> dict[str, Any]:
    """Write a package that runs without the canvas or this database.

    Distinct from `/export` above, which is the JSON backup copy of a workflow
    (the platform's own format, and what re-import consumes). This one produces
    the deliverable: an entry point, vendored runtime, configuration, tests, and a
    Dockerfile.

    Credentials are stripped before anything is written — see
    `runtime/export.py`. The response reports which ones were replaced, so the
    person shipping the package knows what has to be filled in on the other side.
    """
    from app.runtime.export import ExportError, build_export, write_export

    try:
        graph = get_workflow(workflow_id)
    except WorkflowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    settings = get_settings()
    app_root = pathlib.Path(__file__).resolve().parent.parent
    try:
        files = build_export(graph, app_root, runtime_version=settings.runtime_version)
    except ExportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = write_export(files, settings.resolved_agent_export_dir / workflow_id)
    return {
        "path": str(result.root),
        "files": result.files,
        "runtime_hash": result.runtime_hash,
        "graph_hash": result.graph_hash,
        "redacted_credentials": result.redacted_keys,
    }


@router.post(
    "/import",
    status_code=status.HTTP_201_CREATED,
    summary="Import a workflow from an exported payload",
)
def import_graph(
    payload: dict[str, Any], _admin: None = Depends(require_admin)
) -> WorkflowGraph:
    try:
        return import_workflow(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Live port resolution
# ---------------------------------------------------------------------------


class PortsRequest(BaseModel):
    """A node instance, so dynamic ports can be resolved server-side.

    The canvas needs this because a Condition node's output handles depend on its
    configured branches — the frontend cannot derive them from the node type
    alone without duplicating backend logic.
    """

    node: dict[str, Any]


@router.post("/ports", summary="Resolve a node instance's ports")
def ports(request: PortsRequest) -> dict[str, Any]:
    from app.graph.schema import NodeSpec

    try:
        node = NodeSpec.model_validate(request.node)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "inputs": [p.model_dump(mode="json") for p in effective_inputs(node)],
        "outputs": [p.model_dump(mode="json") for p in effective_outputs(node)],
    }
