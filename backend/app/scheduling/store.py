"""Storage for schedules — CRUD over the ``schedules`` table.

A schedule is **a stored trigger plus a workflow id**. This module is only the
row: insert, read, patch, delete, and the two mutations the scheduler performs.

Why the table exists at all, given APScheduler can store jobs
------------------------------------------------------------

APScheduler's ``SQLAlchemyJobStore`` can persist jobs, and using it would mean one
less table. It is not used, for three reasons that are specific to this project:

* **The job store's table is outside alembic.** This project runs ``upgrade head``
  at startup and has a test asserting the migrated schema matches the models. A
  table APScheduler creates lazily is a second, unmanaged source of schema truth.
* **The workflow can be deleted.** APScheduler has no concept of "the thing this
  job points at no longer exists", so reconciliation — the one piece of this
  feature that is genuinely ours — needs a row we control.
* **The audit fields are ours.** Which run a fire produced, how many fire times
  were missed, the last error. APScheduler reports through events, but those are
  process-local and lost on restart; a schedule's reliability record should not be.

So this table is the source of truth and APScheduler holds the schedule *in
memory* (``MemoryJobStore``), re-registered from here at every startup. Nothing is
lost by that: the rule lives in this row, and the next fire time is derived from it.

Two timestamps, and the difference between them matters
--------------------------------------------------------

``last_fired_at`` is when the schedule was *supposed* to fire; ``last_started_at``
is when a run was actually created for it. They are equal normally and diverge when
a missed run is skipped. Keeping both is what makes "this schedule has been firing
late" visible.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from app.db.models import Schedule
from app.db.session import session_scope
from app.scheduling.rules import as_utc

UTC = timezone.utc

#: A schedule pointed at an existing workflow.
SCHEDULE_ACTIVE = "active"
#: A schedule whose workflow no longer exists. Kept, not deleted — see
#: `reconcile`.
SCHEDULE_ORPHANED = "orphaned"

#: Fires on time. Needs a `rule`.
KIND_SCHEDULE = "schedule"
#: Fires when something POSTs to its token. Needs a `token`, and has no rule.
KIND_WEBHOOK = "webhook"
KINDS = (KIND_SCHEDULE, KIND_WEBHOOK)

#: Bytes of entropy in a webhook token. 32 bytes is 256 bits, and the token is the
#: *only* credential an inbound request carries — there is no signature and no
#: second factor, so it has to be infeasible to guess rather than merely unlikely.
#: `token_urlsafe` gives a URL-safe alphabet, which is why it is preferred over
#: hex: same entropy, two thirds the length.
TOKEN_BYTES = 32

#: What to do about fire times that elapsed while the platform was down.
#:
#: `skip` is the default, matching n8n and matching APScheduler's `coalesce=True`.
#: The alternative multiplies load exactly when the platform is least able to take
#: it: down for a weekend under an hourly rule, `catch_up` returns to 48 queued
#: runs competing with whatever a person just asked for.
#:
#: Either way the misses are **recorded**. This decides whether to run the backlog,
#: never whether to admit it was missed — an unrecorded missed run is the failure
#: mode that makes a schedule untrustworthy, because its only symptom is work that
#: did not happen.
MISSED_POLICIES = ("skip", "catch_up")


class ScheduleNotFound(LookupError):
    """Raised by the write operations when a schedule id does not exist.

    The read operations return ``None`` / ``[]`` instead: the decision to answer
    404 belongs to the HTTP layer, not here.
    """


def _record(row: Schedule) -> dict[str, Any]:
    """A row as a plain dictionary.

    Built field by field rather than with ``model_dump``: this dict is the shape
    the scheduler and the API both consume, and spelling it out means a new column
    has to be added here deliberately instead of leaking into every response.
    """
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "name": row.name,
        "kind": row.kind,
        "token": row.token,
        "rule": dict(row.rule or {}),
        "inputs": dict(row.inputs or {}),
        "enabled": row.enabled,
        "status": row.status,
        "status_reason": row.status_reason,
        "dtstart": row.dtstart,
        "next_run_at": row.next_run_at,
        "last_fired_at": row.last_fired_at,
        "last_started_at": row.last_started_at,
        "last_run_id": row.last_run_id,
        "last_error": row.last_error,
        "missed_count": row.missed_count,
        "failure_count": row.failure_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_schedules() -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.exec(select(Schedule).order_by(Schedule.created_at.desc())).all()
        return [_record(row) for row in rows]


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        return None if row is None else _record(row)


def live_schedules() -> list[dict[str, Any]]:
    """Time-based schedules APScheduler should hold a job for.

    Enabled, active, **and of kind `schedule`**. The kind filter is not
    incidental: a webhook has no rule, so registering a job for one would either
    raise on the unparseable rule or — worse, if the rule happened to parse —
    fire it on a timer nobody asked for. A webhook's trigger is an HTTP request,
    not a clock, so it is excluded here and served by `find_by_token` instead.

    An orphaned or paused schedule must cost nothing to have lying around, which
    is why this filters in SQL rather than letting the caller skip rows.
    """
    with session_scope() as session:
        rows = session.exec(
            select(Schedule)
            .where(Schedule.enabled == True)  # noqa: E712 - SQLModel needs the operator
            .where(Schedule.status == SCHEDULE_ACTIVE)
            .where(Schedule.kind == KIND_SCHEDULE)
            .order_by(Schedule.created_at)
        ).all()
        return [_record(row) for row in rows]


def find_by_token(token: str) -> dict[str, Any] | None:
    """The enabled, live webhook trigger for this token, or ``None``.

    Returns ``None`` for a token that exists but is disabled or orphaned, which is
    deliberate: the alternative is an inbound request learning that it found a
    real trigger that is merely switched off. "No such hook" and "that hook is not
    running" are the same answer to someone who should not have the token, and
    telling them apart is free information.

    Timed against a 256-bit token, so an index lookup is not a weaker check than a
    constant-time comparison would be — there is nothing to guess.
    """
    if not token:
        return None
    with session_scope() as session:
        row = session.exec(
            select(Schedule)
            .where(Schedule.token == token)
            .where(Schedule.kind == KIND_WEBHOOK)
            .where(Schedule.enabled == True)  # noqa: E712 - SQLModel needs the operator
            .where(Schedule.status == SCHEDULE_ACTIVE)
        ).first()
        return None if row is None else _record(row)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_schedule(
    *,
    workflow_id: str,
    rule: dict[str, Any] | None = None,
    kind: str = KIND_SCHEDULE,
    name: str = "",
    inputs: dict[str, Any] | None = None,
    enabled: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Insert a trigger. The first fire time is filled in by the service.

    ``dtstart`` is set to the creation instant and never moves afterwards: it is
    the anchor for every field the author left out, so moving it would silently
    reinterpret the rule.

    A webhook's token is generated **here** rather than accepted from the caller.
    A caller-supplied token is a value the server cannot check for weakness or for
    collision with an existing one, and the failure that permits is a guessable
    credential whose owner has no way to tell it is guessable.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}.")
    moment = as_utc(now or datetime.now(UTC))
    with session_scope() as session:
        row = Schedule(
            id=uuid.uuid4().hex[:12],
            workflow_id=workflow_id,
            name=name,
            kind=kind,
            token=secrets.token_urlsafe(TOKEN_BYTES) if kind == KIND_WEBHOOK else "",
            rule=dict(rule or {}),
            inputs=dict(inputs or {}),
            enabled=enabled,
            status=SCHEDULE_ACTIVE,
            dtstart=moment,
            created_at=moment,
            updated_at=moment,
        )
        session.add(row)
        return _record(row)


def update_schedule(
    schedule_id: str,
    *,
    name: str | None = None,
    rule: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    enabled: bool | None = None,
    workflow_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Patch a schedule.

    **Does not recompute `next_run_at`.** That is the service's job, because it is
    the only thing that holds the APScheduler job to move. An earlier version
    computed it here and the result was two places that could disagree about when
    a schedule fires.

    Re-pointing ``workflow_id`` clears an orphaned status: that is the repair such
    a schedule needs, and requiring a separate call would make fixing a broken
    schedule a different gesture from changing one.
    """
    moment = as_utc(now or datetime.now(UTC))
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is None:
            raise ScheduleNotFound(f"No schedule with id '{schedule_id}'.")

        if name is not None:
            row.name = name
        if inputs is not None:
            row.inputs = dict(inputs)
        if rule is not None:
            row.rule = dict(rule)
        if enabled is not None:
            row.enabled = enabled
        if workflow_id is not None:
            row.workflow_id = workflow_id
            # After the assignments above, so it wins over a status carried in the
            # same request. Nothing sends both today; the order is stated so that
            # stays true on purpose rather than by luck.
            row.status = SCHEDULE_ACTIVE
            row.status_reason = ""
        row.updated_at = moment
        session.add(row)
        return _record(row)


def set_next_run(schedule_id: str, next_run_at: datetime | None) -> None:
    """Record what APScheduler decided the next fire is.

    Written from the scheduler's own ``job.next_run_time`` rather than recomputed,
    so the value on the list page is the one the loop will actually act on. Two
    computations of "when next" is how a UI shows a time that never arrives.
    """
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is not None:
            row.next_run_at = None if next_run_at is None else as_utc(next_run_at)
            session.add(row)


def mark_fired(schedule_id: str, *, intended: datetime) -> None:
    """Move ``last_fired_at`` forward. Called before anything else is attempted."""
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is not None:
            row.last_fired_at = as_utc(intended)
            session.add(row)


def record_started(schedule_id: str, run_id: str) -> None:
    """A run was successfully started. The only branch that clears the failure count."""
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is not None:
            row.last_run_id = run_id
            row.last_started_at = datetime.now(UTC)
            row.last_error = ""
            row.failure_count = 0
            session.add(row)


def record_error(schedule_id: str, error: str) -> None:
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is not None:
            row.last_error = error
            row.failure_count += 1
            session.add(row)


def note_missed(schedule_id: str, count: int) -> None:
    """Add to the missed total. Says nothing about any run.

    Separate from :func:`record_started` because a miss has to be recorded *before*
    the run is attempted, so it survives that attempt failing, and recording it
    must not look like the attempt succeeded. Added, never assigned, and never
    reset by a later success: a schedule that recovered has still missed those
    moments, and that is information about the platform's reliability which a good
    night does not erase.
    """
    if count <= 0:
        return
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is not None:
            row.missed_count += count
            session.add(row)


def delete_schedule(schedule_id: str) -> bool:
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is None:
            return False
        session.delete(row)
        return True


def rotate_token(schedule_id: str) -> dict[str, Any]:
    """Issue a fresh webhook token for this trigger.

    The only remedy for a leaked hook URL. Rebuilding the trigger instead would
    lose the run history attached to the row and the inputs it was configured with,
    which is a heavy price for rotating a string.

    Returns the updated row, so the caller can show the new URL. That is the one
    place the token is echoed back, and it is deliberate: the operator has just
    invalidated the old one and has nothing that works until they see this.
    """
    with session_scope() as session:
        row = session.get(Schedule, schedule_id)
        if row is None:
            raise ScheduleNotFound(f"No schedule with id '{schedule_id}'.")
        if row.kind != KIND_WEBHOOK:
            raise ValueError("only a webhook trigger has a token to rotate.")
        row.token = secrets.token_urlsafe(TOKEN_BYTES)
        row.updated_at = datetime.now(UTC)
        session.add(row)
        return _record(row)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(
    existing_workflow_ids: set[str], *, now: datetime | None = None
) -> dict[str, int]:
    """Retire schedules whose workflow has gone, and revive the ones that came back.

    Runs at startup rather than on a timer. A workflow cannot disappear while this
    process is the only writer, so checking periodically is a query per interval to
    detect something that cannot have changed. Startup is what makes the state
    correct after the case that actually happens: someone deletes a workflow while
    the platform is down.

    **Kept, not deleted.** Deleting would erase the schedule's intent along with
    its target, and the repair — point it at the new workflow — is the same edit as
    any other change. An orphaned row is also visible on the list page, which a row
    that no longer exists is not.
    """
    moment = as_utc(now or datetime.now(UTC))
    orphaned = revived = 0
    with session_scope() as session:
        for row in session.exec(select(Schedule)).all():
            present = row.workflow_id in existing_workflow_ids
            if not present and row.status != SCHEDULE_ORPHANED:
                row.status = SCHEDULE_ORPHANED
                row.status_reason = (
                    f"workflow '{row.workflow_id}' no longer exists; "
                    "point this schedule at a workflow to re-enable it"
                )
                row.next_run_at = None
                row.updated_at = moment
                session.add(row)
                orphaned += 1
            elif present and row.status == SCHEDULE_ORPHANED:
                row.status = SCHEDULE_ACTIVE
                row.status_reason = ""
                row.updated_at = moment
                session.add(row)
                revived += 1
    return {"orphaned": orphaned, "revived": revived}
