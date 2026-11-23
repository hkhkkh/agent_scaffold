"""Workflow persistence: SQLite rows plus git-friendly JSON exports."""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete
from sqlmodel import select

from app.db.models import Run as RunRow
from app.db.models import RunStep as RunStepRow
from app.db.models import Workflow, WorkflowVersion
from app.db.session import session_scope
from app.graph.schema import WorkflowGraph
from app.graph.subgraphs import definition_digest, pin_references
from app.settings import get_settings


def load_workflow_or_none(workflow_id: str) -> WorkflowGraph | None:
    """A workflow, or ``None`` when there is no such one.

    The sub-flow provider's shape. Returning ``None`` rather than raising keeps
    "not found" a normal answer: the compiler and the validator both want to
    report it with a message that names the referencing node, and a raised
    exception would be caught and re-worded in two places.
    """
    with session_scope() as session:
        row = session.get(Workflow, workflow_id)
        if row is None or not row.graph:
            return None
        return WorkflowGraph.model_validate(row.graph)


def list_workflow_options() -> list[dict[str, Any]]:
    """Workflows as dropdown entries for the sub-flow node's selector."""
    return [
        {
            "value": item["id"],
            "label": item["name"],
            "description": item.get("description") or "",
        }
        for item in list_workflows()
    ]


class WorkflowNotFound(LookupError):
    """Raised when a workflow id does not exist."""


class WorkflowVersionNotFound(LookupError):
    """Raised when a version of a workflow does not exist.

    Distinct from :class:`WorkflowNotFound` because the two need different
    answers: the HTTP layer turns one into 404 (no such workflow) and the other
    into 404 with a different message (that workflow has no such version).
    Collapsing them would tell a caller their workflow id was wrong when only the
    version was.
    """


def list_workflows() -> list[dict[str, Any]]:
    """Summaries for the workflow picker — no graph payloads."""
    from app.settings import get_settings

    demo = get_settings().public_demo
    with session_scope() as session:
        rows = session.exec(select(Workflow).order_by(Workflow.updated_at.desc())).all()
        summaries = []
        for row in rows:
            graph = row.graph or {}
            has_code = any(
                node.get("type") == "code" for node in graph.get("nodes", [])
            )
            has_tools = bool(graph.get("tools"))
            summaries.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "description": row.description,
                    "version": row.version,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "node_count": len(graph.get("nodes", [])),
                    # A display-only workflow in the public demo. The UI uses this
                    # to show a badge and disable Run/Save before the user tries.
                    "read_only": bool(demo and (has_code or has_tools)),
                }
            )
        return summaries


def workflow_exists(workflow_id: str) -> bool:
    with session_scope() as session:
        return session.get(Workflow, workflow_id) is not None


def count_workflows() -> int:
    with session_scope() as session:
        return len(session.exec(select(Workflow)).all())


def get_workflow(workflow_id: str) -> WorkflowGraph:
    with session_scope() as session:
        row = session.get(Workflow, workflow_id)
        if row is None:
            raise WorkflowNotFound(f"No workflow with id '{workflow_id}'.")
        return _to_graph(row)


def get_workflow_version(workflow_id: str, version: int) -> WorkflowGraph:
    """One historical version of a definition.

    This is the whole point of the feature: the definition that ran six months ago
    is still that definition, so a run can be reproduced after the canvas has moved
    on. Raises rather than falling back to the current version — silently answering
    with a different definition than the one asked for would be worse than failing.
    """
    with session_scope() as session:
        row = session.exec(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .where(WorkflowVersion.version == version)
        ).first()
        if row is None:
            raise WorkflowVersionNotFound(
                f"Workflow '{workflow_id}' has no version {version}."
            )
        return WorkflowGraph.model_validate(row.graph)


def list_workflow_versions(workflow_id: str) -> list[dict[str, Any]]:
    """Every version of a definition, newest first — without the graph payloads.

    Summaries only: a workflow with fifty versions would otherwise return fifty
    whole graphs to render a list of dates. The payload is fetched per version by
    :func:`get_workflow_version` when someone actually asks for one.
    """
    with session_scope() as session:
        if session.get(Workflow, workflow_id) is None:
            raise WorkflowNotFound(f"No workflow with id '{workflow_id}'.")
        rows = session.exec(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())  # type: ignore[union-attr]
        ).all()
        return [
            {
                "version": row.version,
                "definition_hash": row.definition_hash,
                "name": row.name,
                "saved_by": row.saved_by,
                "created_at": row.created_at,
                # Why this version exists, when it came from the copilot. Opaque
                # to the repository; the UI decides what to show.
                "meta": row.meta or {},
                # Enough to show what changed without shipping the graph.
                "node_count": len((row.graph or {}).get("nodes", [])),
                "edge_count": len((row.graph or {}).get("edges", [])),
            }
            for row in rows
        ]


def current_definition_hash(workflow_id: str) -> str:
    """The hash of the definition as saved, or ``""`` when there is no such row.

    Reads the denormalised column rather than re-hashing ``graph``: this is what
    the sub-flow pin check needs, and it runs on every compile.
    """
    with session_scope() as session:
        row = session.get(Workflow, workflow_id)
        return (row.definition_hash or "") if row is not None else ""


def save_workflow(
    graph: WorkflowGraph, *, meta: dict[str, Any] | None = None
) -> WorkflowGraph:
    """Create or replace a workflow, and refresh its JSON export.

    Saving is a full replace rather than a merge: the canvas is the source of
    truth, and a merge would make a deleted node reappear.

    ``meta`` is stored on the new version row, not in the graph. It answers
    "why does this version exist" for an AI edit without becoming part of the
    definition that runs or exports.

    Sub-flow references are pinned to the digest of the definition they point at
    here, on the way in. Doing it at save time is what makes the guarantee hold
    without the user taking an extra step: from the moment this graph is saved,
    editing the sub-workflow it uses is reported by the compiler instead of
    silently changing what this graph does.
    """
    graph = pin_references(graph)
    graph.updated_at = datetime.now(timezone.utc)
    digest = definition_digest(graph)
    payload = graph.model_dump(mode="json")

    with session_scope() as session:
        row = session.get(Workflow, graph.id)
        if row is None:
            # The version number comes from the history table, not from the client.
            # `graph.version` is whatever the canvas last saw, and honouring it
            # would let two open editors both save "version 3".
            row = Workflow(
                id=graph.id,
                name=graph.name,
                description=graph.description,
                version=0,
                definition_hash=digest,
                graph=payload,
                created_at=graph.created_at,
            )
            session.add(row)
        else:
            row.name = graph.name
            row.description = graph.description
            row.definition_hash = digest
            row.graph = payload
            row.updated_at = graph.updated_at
            session.add(row)

        # The highest existing number, read inside this transaction, so two
        # concurrent saves cannot both write the same one. The unique constraint on
        # (workflow_id, version) is the backstop if this ever races anyway.
        highest = session.exec(
            select(WorkflowVersion.version)
            .where(WorkflowVersion.workflow_id == graph.id)
            .order_by(WorkflowVersion.version.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        next_version = int(highest or 0) + 1

        # A snapshot, deliberately. Not a delta: reconstructing one version from a
        # chain of patches means a single missing row breaks every version after
        # it. See `WorkflowVersion` in `app/db/models.py`.
        session.add(
            WorkflowVersion(
                workflow_id=graph.id,
                version=next_version,
                definition_hash=digest,
                name=graph.name,
                graph=payload,
                meta=dict(meta or {}),
            )
        )

        row.version = next_version
        graph.version = next_version

    export_workflow(graph)
    return graph


def delete_workflow(workflow_id: str) -> None:
    """Delete a workflow along with its runs, steps, and versions.

    Done explicitly because neither ``runs.workflow_id`` nor
    ``workflow_versions.workflow_id`` is a foreign key (draft runs and draft
    definitions may reference a workflow that was never saved), so the database
    will not cascade for us.

    **Versions go too.** They exist to reproduce a run of *this* workflow, so once
    the workflow is gone they have no reader. Keeping them would mean an operator
    deleting a definition and finding its history still growing on disk.

    A constant number of statements, however many runs the workflow has. The first
    version walked the runs one at a time and, inside each, loaded every step row
    only in order to delete it — work the ``RunRow.steps`` relationship already
    does on its own: it declares ``cascade="all, delete-orphan"`` over a real
    foreign key, so deleting the run removed those steps regardless. Deleting by
    ``run_id IN (...)`` says the same thing once.
    """
    with session_scope() as session:
        run_ids = session.exec(
            select(RunRow.id).where(RunRow.workflow_id == workflow_id)
        ).all()

        if run_ids:
            # Steps first: `run_steps.run_id` is a foreign key, so the order is
            # what keeps this honest on a database that enforces them.
            session.execute(delete(RunStepRow).where(RunStepRow.run_id.in_(run_ids)))
            session.execute(delete(RunRow).where(RunRow.id.in_(run_ids)))

        session.execute(
            delete(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id)
        )

        row = session.get(Workflow, workflow_id)
        if row is not None:
            session.delete(row)

    path = _artifact_path(workflow_id)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# File export
# ---------------------------------------------------------------------------

#: Matches the constraint on `WorkflowGraph.id`. Duplicated deliberately rather
#: than imported: this is a *filesystem* guard, and it must keep holding even if
#: someone later relaxes the schema for some other purpose.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _artifact_path(workflow_id: str) -> pathlib.Path:
    """Resolve ``data/workflows/<id>.json``, refusing anything that escapes it.

    The schema already rejects unsafe ids, so this is defence in depth — the kind
    that matters because a path built by string interpolation is one relaxed
    validator away from being arbitrary file write.
    """
    if not _SAFE_ID.match(workflow_id or ""):
        raise ValueError(
            f"Refusing to build a path from workflow id {workflow_id!r}: "
            "only letters, digits, dash, and underscore are allowed."
        )
    return get_settings().workflows_dir / f"{workflow_id}.json"


def export_workflow(graph: WorkflowGraph) -> str:
    """Write a JSON copy of the workflow to ``data/workflows/<id>.json``.

    Kept in sync on every save so a workflow is diffable in git and recoverable
    if the database is lost — the database is a cache, the file is the artifact.
    """
    settings = get_settings()
    settings.ensure_dirs()
    path = _artifact_path(graph.id)
    path.write_text(
        json.dumps(graph.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


def import_workflow(payload: dict[str, Any]) -> WorkflowGraph:
    """Validate and save a workflow from an exported JSON payload."""
    graph = WorkflowGraph.model_validate(payload)
    return save_workflow(graph)


def _to_graph(row: Workflow) -> WorkflowGraph:
    """Rebuild a graph from a stored row.

    Rows written before a schema change may not validate strictly, so a failure
    here reports the workflow id rather than surfacing an opaque pydantic error.
    """
    try:
        return WorkflowGraph.model_validate(row.graph or {})
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"Workflow '{row.id}' could not be loaded: the stored definition does "
            f"not match the current schema ({exc})."
        ) from exc


__all__ = [
    "WorkflowNotFound",
    "delete_workflow",
    "export_workflow",
    "get_workflow",
    "import_workflow",
    "list_workflows",
    "save_workflow",
]
