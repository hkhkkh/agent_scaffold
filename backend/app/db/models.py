"""Database models.

Storage strategy
----------------
Two stores, each doing what it is good at:

* **SQLite** holds workflows, runs, and per-step results. It gives us indexed
  queries (list recent runs, filter by status) and atomic updates, which a pile
  of JSON files cannot.
* **JSON files** hold two things SQLite is bad at: a human-readable, git-friendly
  export of each workflow (``data/workflows/<id>.json``) and the full event
  transcript of a run (``data/runs/<id>.json``), which is append-heavy and only
  ever read whole.

Graph payloads are stored as JSON columns rather than normalised tables. A graph
is always read and written as a unit, and normalising nodes/edges would buy
nothing while making every read a join.

Note: **no** ``from __future__ import annotations`` here. SQLModel resolves
relationship targets from the runtime annotation value, and a stringised
annotation makes it treat ``List["Run"]`` as an opaque string and fail at mapper
configuration time.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import JSON, Column, DateTime, TypeDecorator, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class UtcDateTime(TypeDecorator):
    """A ``DateTime`` that always hands back a timezone-aware UTC value.

    SQLite has no native datetime type. SQLAlchemy stores one as a string and
    parses it back keeping the wall-clock fields but **dropping the offset**, so
    every datetime read out of this database came back naive while every datetime
    in memory was aware. Everything written here is UTC (see :func:`_now`), so
    the value was right and merely unlabelled -- but an unlabelled datetime is not
    the same value as a labelled one:

    * ``datetime(2026, 9, 14) == datetime(2026, 9, 14, tzinfo=timezone.utc)`` is
      ``False``, so two paths could hold the same instant and disagree.
    * ``sorted([naive, aware])`` raises ``TypeError: can't compare offset-naive
      and offset-aware datetimes``.

    The second one was a **real 500**, not a theoretical one. A run rehydrated
    after a restart (see ``RunManager.hydrate``) carried a naive ``created_at``
    into the manager's live-run map, next to runs that carried aware ones, and
    ``GET /runs`` sorts that map by ``created_at``. Measured before it was fixed:
    ``manager.list()`` raised ``TypeError`` with one rehydrated run present.

    Fixed on the column rather than in each reader because there is exactly one
    reason the offset disappears and it belongs to the type. Patching readers
    would be one copy of this knowledge per read path -- and this codebase has
    already been bitten more than once by two read paths drifting apart.

    Attaching UTC is not a guess. The only writer is :func:`_now`, which is aware
    UTC, so an offset-less value can only be a UTC one with its label removed.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Workflow(SQLModel, table=True):
    """A saved workflow definition.

    This row is the **current** definition. Its history lives in
    :class:`WorkflowVersion` — one immutable row per save — so this table keeps its
    existing shape and every query against it keeps working unchanged.
    """

    __tablename__ = "workflows"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str = "Untitled workflow"
    description: str = ""
    version: int = 1

    #: The serialised ``WorkflowGraph``. Stored whole; see module docstring.
    graph: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))

    #: The content hash of ``graph`` as it stands. Denormalised from the version
    #: row so "has this definition changed at all?" is answerable without touching
    #: the history table, which is the question the sub-flow pin check asks on
    #: every compile.
    #:
    #: Nullable rather than defaulted: rows written before this column existed have
    #: no hash, and filling them with one computed now would claim a definition was
    #: pinned at a moment it was not.
    definition_hash: Optional[str] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)
    updated_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)

    #: No ORM relationship to Run. `runs.workflow_id` is not a foreign key (see
    #: Run below), so SQLAlchemy cannot infer the join, and every query in this
    #: codebase is an explicit `select(...).where(...)` anyway.


class WorkflowVersion(SQLModel, table=True):
    """One saved definition, kept forever.

    Why a separate table rather than a `version` column on ``Workflow``
    ------------------------------------------------------------------

    Task 28's own note says it: *"别把版本历史塞进同一张表"*. A version column on
    the existing row forces either a composite primary key — which breaks every
    query in this codebase — or a second "which row is current" flag to filter on
    everywhere. A history table leaves ``Workflow`` exactly as it is: one row per
    workflow, still the current one, still answered by the same ``session.get``.

    The pattern is SQLAlchemy's own ``examples/versioned_history`` — a history
    table plus a monotonically increasing version number. Adopted as a *pattern*,
    not as a dependency: that example is shipped under ``/examples/``, is not
    importable as a library, and the `sqlalchemy-history`-style packages that do
    exist would create their own tables outside alembic, which this project has a
    test forbidding (``TestTheTwoPathsAgree``).

    **Snapshots, not deltas.** Each row holds the whole definition. A delta chain
    would be smaller, but reconstructing one version would require replaying every
    earlier one, so a single corrupt or missing row would break every version after
    it. A snapshot has no such failure mode, and at this size (tens of KB per
    graph) the storage is not worth optimising. LangSmith's assistant versioning
    documents the same choice as a warning to its users: updates create versions
    "from scratch and do not merge".

    **Never updated, never deleted.** There is no code path that modifies a row
    here after insert. That is what makes `get(id, version=1)` a promise: the
    definition that ran six months ago is still exactly that definition.
    """

    __tablename__ = "workflow_versions"
    #: Enforces "one row per version" — the reason a synthetic `id` primary key is
    #: safe. Without it, a retry that re-ran the insert would silently create a
    #: second row with the same number, and `get(id, version=N)` would return
    #: whichever the database happened to order first.
    __table_args__ = (UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),)

    #: ``(workflow_id, version)`` is the real key, but SQLModel wants a single
    #: primary key and a synthetic one costs nothing. The unique constraint below is
    #: what actually enforces "one row per version".
    id: Optional[int] = Field(default=None, primary_key=True)

    #: Not a foreign key, matching the rest of this schema: a definition can be
    #: saved before any ``Workflow`` row exists for it (the canvas saves an unsaved
    #: draft under a client-generated id). Deleting a workflow removes its versions
    #: explicitly, the same way it removes its runs.
    workflow_id: str = Field(index=True)

    #: 1-based, incremented on each save. Assigned from ``max(version) + 1`` inside
    #: the same transaction that inserts, so two saves racing cannot claim the same
    #: number.
    version: int = Field(index=True)

    #: The content hash, same function the sub-flow pins use. Stored so a version
    #: can be found by content ("which version had this exact definition?") without
    #: re-hashing every row.
    definition_hash: str = Field(default="", index=True)

    name: str = ""
    #: The serialised ``WorkflowGraph`` at this version. The whole thing.
    graph: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))

    #: Who saved it, when the platform knows. Empty for the local single-user mode,
    #: which is the default — so this is a breadcrumb, not an audit trail. A real
    #: audit trail is task 26 and would need much more than a name.
    saved_by: str = ""

    #: Optional record of the AI edit that produced this version: the user's
    #: request, the mode, the plan, the assumptions, and the trial result. Kept
    #: as opaque JSON so the shape can grow without another migration. The graph
    #: snapshot above stays the authoritative definition; this only explains how
    #: it got there.
    meta: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=True)
    )

    created_at: datetime = Field(default_factory=_now, index=True, sa_type=UtcDateTime)


class Run(SQLModel, table=True):
    """One execution of a workflow."""

    __tablename__ = "runs"

    id: str = Field(default_factory=_uuid, primary_key=True)
    #: Deliberately **not** a foreign key. A workflow can be run before it has
    #: been saved — that is how you test a draft — so a run may legitimately
    #: reference a workflow id that was never persisted. Deleting a workflow
    #: removes its runs explicitly (see the workflows API).
    workflow_id: str = Field(index=True)

    status: str = Field(default="pending", index=True)
    inputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))
    outputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))
    error: Optional[str] = None

    #: LangGraph thread id — the handle used to resume an interrupted run.
    thread_id: str = Field(default_factory=_uuid, index=True)

    #: Per-run config overrides. Persisted so an interrupted run can be rebuilt in
    #: a **new process** — the run that comes back must use the same tweaks it
    #: started with, or it silently continues under defaults and produces a
    #: different answer with nothing reporting an error.
    tweaks: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))
    #: The graph's step ceiling for this run, persisted for the same reason.
    #:
    #: Nullable rather than defaulted: ``None`` means "this row predates the
    #: column", and a rehydrated run then falls back to the configured default.
    #: Filling it with a number would make an unknown indistinguishable from a
    #: deliberate choice.
    recursion_limit: Optional[int] = None

    created_at: datetime = Field(default_factory=_now, index=True, sa_type=UtcDateTime)
    finished_at: Optional[datetime] = Field(default=None, sa_type=UtcDateTime)

    #: Cascade-deleting steps with their run is the one relationship worth
    #: keeping: it guarantees a run never leaves orphaned step rows behind.
    #: (``runs.workflow_id`` has no ORM relationship — see the field comment.)
    steps: List["RunStep"] = Relationship(
        back_populates="run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class RunStep(SQLModel, table=True):
    """One node execution within a run — the unit the UI timeline renders."""

    __tablename__ = "run_steps"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True)
    node_id: str = Field(index=True)
    node_label: str = ""

    #: Monotonic counter so the timeline keeps its order even when steps share a
    #: timestamp (parallel branches).
    index: int = 0

    status: str = "running"
    inputs: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    outputs: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: Optional[str] = None
    #: Lines the node emitted with `ctx.log(...)`. A JSON list rather than a
    #: child table: they are only ever read as a unit with the step, and a table
    #: would add a join per step row to every timeline render.
    logs: Optional[list[str]] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    started_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)
    finished_at: Optional[datetime] = Field(default=None, sa_type=UtcDateTime)
    duration_ms: Optional[float] = None

    run: Optional["Run"] = Relationship(back_populates="steps")


class Schedule(SQLModel, table=True):
    """A stored trigger that starts an ordinary run when its moment arrives.

    Two kinds of trigger share this table, which is what the line above is really
    saying: a *stored trigger plus a workflow id*. ``kind='schedule'`` fires on
    time and carries a ``rule``; ``kind='webhook'`` fires when an external system
    POSTs to its ``token``.

    One table rather than two because everything except the firing mechanism is
    identical: the workflow it points at, the orphan check when that workflow is
    deleted, the run history it produces, the enabled flag. A second table would
    be a second copy of all of it.

    Declared here rather than beside the code that uses it. An earlier version put
    it in ``app/runtime/`` because that is where the concept lives, and the cost
    was an extra module whose only job was importing this class so
    ``SQLModel.metadata`` could see it — plus edits to ``create_all`` and
    ``alembic/env.py``. A model's home is ``models.py``; the concept's home is the
    feature package, and the two do not have to be the same file.

    The behaviour lives in ``app/scheduling/``. This class is the shape of a row.
    """

    __tablename__ = "schedules"

    id: str = Field(default_factory=_uuid, primary_key=True)
    #: Not a foreign key, matching `Run.workflow_id` and for the same reason: a
    #: schedule may be pointed at a draft. Unlike a run, a schedule outlives the
    #: moment it was created, so a workflow deleted out from under it has to be
    #: noticed — that is what `status` is for.
    workflow_id: str = Field(index=True)
    name: str = ""

    #: `schedule` (fires on time, needs `rule`) or `webhook` (fires on a POST,
    #: needs `token`). Indexed because the list page and the job registration both
    #: filter on it.
    kind: str = Field(default="schedule", index=True)

    #: The credential for a webhook trigger, and the whole of it — anyone holding
    #: this string can start a run. Generated with `secrets.token_urlsafe`, never
    #: derived from the id, and empty for a time-based row.
    #:
    #: Indexed because lookup-by-token is the hot path: an inbound request has
    #: nothing else to identify itself with.
    token: str = Field(default="", index=True)

    #: The serialised rule, JSON rather than one column per field: the fields
    #: differ by frequency (weekly has weekdays, monthly has a day of month), so
    #: columns would be nullable ones whose validity depends on a *different*
    #: column, which SQLite will not enforce for us. Empty for a webhook row.
    rule: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))
    #: The inputs a scheduled run is given, so it is reproducible from this row.
    inputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=True))
    enabled: bool = Field(default=True, index=True)

    #: ``active`` or ``orphaned`` (see ``app/runtime/schedules.py``). An orphaned
    #: schedule is kept, not deleted: its intent is still recorded, so pointing it
    #: at a new workflow restores it.
    status: str = Field(default="active", index=True)
    status_reason: str = ""

    #: Anchor for the rule's defaults ("daily" created at 09:00 means 09:00 daily).
    #: Set once at creation and never moved — moving it would silently reinterpret
    #: every field the author left empty.
    dtstart: datetime = Field(default_factory=_now, sa_type=UtcDateTime)

    #: When the next fire is due. Nullable is load-bearing: ``None`` means "no
    #: future occurrence", which is how a rule that can never match again is
    #: retired instead of being re-queried forever. Indexed because the
    #: scheduler's per-tick query is the one read path in this schema that loops.
    next_run_at: Optional[datetime] = Field(default=None, index=True, sa_type=UtcDateTime)

    #: Two timestamps, and the difference between them is the point.
    #: ``last_fired_at`` is when the schedule was *supposed* to fire;
    #: ``last_started_at`` is when a run was actually created for it. They are
    #: equal normally and diverge when a missed run is caught up. Catch-up walks
    #: from the first, so collapsing them would skip the backlog behind a single
    #: failure.
    last_fired_at: Optional[datetime] = Field(default=None, sa_type=UtcDateTime)
    last_started_at: Optional[datetime] = Field(default=None, sa_type=UtcDateTime)
    last_run_id: str = ""
    last_error: str = ""

    #: Two counters, kept apart because their causes and repairs differ: a miss
    #: is usually the platform having been down, a failure is usually the
    #: workflow being broken. Neither is reset by a later success.
    missed_count: int = 0
    #: Consecutive failures *starting a run*. This one is reset by a success.
    failure_count: int = 0

    created_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)
    updated_at: datetime = Field(default_factory=_now, sa_type=UtcDateTime)


__all__ = [
    "Run",
    "RunStep",
    "Schedule",
    "UtcDateTime",
    "Workflow",
    "WorkflowVersion",
]
