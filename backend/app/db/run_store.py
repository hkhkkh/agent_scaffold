"""The database-backed :class:`~app.runtime.runs.RunStore`.

This is the platform's implementation. It lives here, beside the models it
writes, rather than in ``app/runtime/`` — see the module docstring in
``app/runtime/runs.py`` for why that split is load-bearing.

The logic is not new. It is the ``_persist_start`` / ``_persist_finish`` bodies
lifted out of ``runtime/manager.py`` and the read queries lifted out of
``api/runs.py``, with the redaction left behind: the manager builds the payloads
and passes them in already safe, so this file only maps dictionaries onto rows.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import select

from app.db.models import Run as RunRow
from app.db.models import RunStep as RunStepRow
from app.db.session import session_scope
from app.runtime.runs import RUN_STORE_SQLITE, RunStore, elapsed_ms
from app.runtime.serialize import final_output


class SqliteRunStore(RunStore):
    """Run history in the platform database. The default.

    Every method opens its own session, matching what the code it replaces did.
    That is worth keeping: a run's ``finish`` happens in a ``finally`` block long
    after the request that started it returned, so there is no request-scoped
    session to borrow.
    """

    name = RUN_STORE_SQLITE

    # ------------------------------------------------------------------ write
    def start(self, run: dict[str, Any]) -> None:
        with session_scope() as session:
            session.add(
                RunRow(
                    id=run["id"],
                    workflow_id=run["workflow_id"],
                    status=run["status"],
                    inputs=run["inputs"],
                    outputs={},
                    thread_id=run.get("thread_id"),
                    # Persisted so an interrupted run can be rebuilt in a new
                    # process with the configuration it started under. See the
                    # field comments in `app/db/models.py`.
                    tweaks=run.get("tweaks") or {},
                    recursion_limit=run.get("recursion_limit"),
                    created_at=run["created_at"],
                )
            )

    def finish(self, run: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        with session_scope() as session:
            row = session.get(RunRow, run["id"])
            if row is not None:
                row.status = run["status"]
                row.outputs = run["outputs"]
                row.error = run.get("error")
                row.finished_at = run.get("finished_at")
                session.add(row)

            for step in steps:
                session.add(
                    RunStepRow(
                        run_id=step["run_id"],
                        node_id=step["node_id"],
                        node_label=step["node_label"],
                        index=step["index"],
                        status=step["status"],
                        inputs=step.get("inputs"),
                        outputs=step.get("outputs"),
                        error=step.get("error"),
                        started_at=step.get("started_at"),
                        finished_at=step.get("finished_at"),
                        duration_ms=step.get("duration_ms"),
                        logs=step.get("logs") or [],
                    )
                )
    # ------------------------------------------------------------------- read
    def exists(self, run_id: str) -> bool:
        with session_scope() as session:
            return session.get(RunRow, run_id) is not None

    def summary(self, run_id: str) -> dict[str, Any] | None:
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return None
            payload = _row_to_payload(row)
        payload["duration_ms"] = elapsed_ms(payload)
        return payload

    def load(self, run_id: str) -> dict[str, Any] | None:
        """The rehydration payload: what `RunManager.hydrate` needs.

        Reads the columns ``summary`` deliberately omits. Kept separate because
        ``summary`` is a wire shape that ``api/runs.py`` compares key-for-key
        against the live-run summary — adding ``tweaks`` to it would both leak an
        internal and break that comparison.
        """
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "workflow_id": row.workflow_id,
                "status": row.status,
                "inputs": row.inputs,
                "thread_id": row.thread_id,
                "tweaks": row.tweaks or {},
                "recursion_limit": row.recursion_limit,
                "created_at": row.created_at,
            }

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        with session_scope() as session:
            rows = session.exec(
                select(RunStepRow)
                .where(RunStepRow.run_id == run_id)
                .order_by(RunStepRow.index)
            ).all()
            return [_step_to_payload(row) for row in rows]

    def history(
        self, workflow_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with session_scope() as session:
            statement = select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
            if workflow_id:
                statement = statement.where(RunRow.workflow_id == workflow_id)
            return [_row_to_payload(row) for row in session.exec(statement).all()]


def _row_to_payload(row: RunRow) -> dict[str, Any]:
    """Shape a ``Run`` row the way a live-run summary is shaped."""
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "status": row.status,
        "inputs": row.inputs,
        "outputs": row.outputs,
        # Derived, not stored: the End node's `output_label` is already a key on
        # `outputs`, so a column would be a second copy of the same fact — and
        # one that could disagree with the payload it describes.
        # See `runtime.serialize.final_output`.
        "final_output": final_output(row.outputs),
        "error": row.error,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


def _step_to_payload(row: RunStepRow) -> dict[str, Any]:
    """Shape a ``RunStep`` row for the wire.

    No redaction: the row was written through ``redact`` already, and doing it
    twice would let the write and read paths drift apart.
    """
    return {
        "node_id": row.node_id,
        "node_label": row.node_label,
        "index": row.index,
        "status": row.status,
        "inputs": row.inputs,
        "outputs": row.outputs,
        "error": row.error,
        "duration_ms": row.duration_ms,
        # `or []` rather than the column: rows written before this field existed
        # have NULL, and a client that has to handle both `null` and `[]` for
        # "no logs" will get it wrong in one of the two places it renders them.
        "logs": row.logs or [],
    }


__all__ = ["SqliteRunStore"]
