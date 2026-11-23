"""Schedule endpoints: list, create, edit, enable/disable, delete.

A plain CRUD surface over the store, plus two operational endpoints. There is no
"run now" endpoint, and its absence is intentional rather than an oversight:
POSTing to ``/runs`` with the schedule's ``workflow_id`` and ``inputs`` is the same
thing, it already reports errors the same way, and it already appears in run
history. A second way to do it would be a second thing to keep in sync.

Every mutation that affects timing goes through ``sync()``, which re-registers the
APScheduler jobs. The API never edits a row and leaves the job alone: that would
leave the schedule firing on the old rule until the next restart, which is the kind
of bug that only shows up in production at 3am.

The wire shape carries two derived fields — ``validity`` and ``description`` — so
the UI does not reimplement the precedence of disabled / orphaned / unreadable and
cannot disagree with the log about which reason applies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.db.repository import WorkflowNotFound, get_workflow
from app.security.policy import require_admin
from app.scheduling import store
from app.scheduling.rules import (
    FREQUENCIES,
    WEEKDAYS,
    ScheduleRule,
    ScheduleRuleError,
    describe_rule,
    rule_error,
)
from app.scheduling.service import get_scheduler

router = APIRouter(
    prefix="/schedules",
    tags=["schedules"],
    dependencies=[Depends(require_admin)],
)

#: Timestamps the wire shape renders as ISO strings.
_STAMPS = (
    "dtstart",
    "next_run_at",
    "last_fired_at",
    "last_started_at",
    "created_at",
    "updated_at",
)


class RuleBody(BaseModel):
    """The rule as it arrives. Validated into a `ScheduleRule` by `to_rule`.

    Kept separate from ``ScheduleRule`` so the wire format can be forgiving (a bare
    int for ``hour`` as well as a list) while the domain object stays strict.
    Parsing ``ScheduleRule`` straight from the body instead means every validation
    error surfaces as a pydantic 422 with a location path, which for "a weekly rule
    must pick days with ``weekday``" is a worse message than the one
    ``ScheduleRule`` writes.
    """

    frequency: str
    interval: int = 1
    minute: int | None = None
    hour: list[int] = Field(default_factory=list)
    weekday: list[int] = Field(default_factory=list)
    day: int | None = None

    def to_rule(self) -> ScheduleRule:
        try:
            return ScheduleRule.from_dict(self.model_dump())
        except ScheduleRuleError as exc:
            # 422 rather than 400: the request is well-formed and understood — it
            # just cannot describe a recurrence. Same category as a run against a
            # graph that will not compile, which also answers 422 here.
            raise HTTPException(status_code=422, detail=str(exc)) from exc


class ScheduleCreate(BaseModel):
    """A new trigger. `schedule` fires on time; `webhook` fires on a POST.

    `rule` is required for the first and ignored for the second, which is checked
    in `_shape` rather than expressed in the type: a discriminated union would say
    it more precisely and would also make the error for a missing rule a pydantic
    location path instead of a sentence.
    """

    workflow_id: str
    kind: str = store.KIND_SCHEDULE
    rule: RuleBody | None = None
    name: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("workflow_id")
    @classmethod
    def _known_workflow(cls, value: str) -> str:
        """Reject a trigger for a workflow that does not exist.

        Checked at create time even though ``reconcile`` would catch it later: a
        trigger stored against a typo'd id is one the user believes is set up, and
        finding out at the first fire is exactly the silent failure this feature
        exists to avoid.
        """
        try:
            get_workflow(value)
        except WorkflowNotFound as exc:
            raise ValueError(str(exc)) from exc
        return value

    def checked(self) -> tuple[str, dict[str, Any]]:
        """The validated ``(kind, rule)``, or a 422 explaining what is missing."""
        if self.kind not in store.KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"kind must be one of {', '.join(store.KINDS)}, got {self.kind!r}.",
            )
        if self.kind == store.KIND_WEBHOOK:
            # A webhook's moment is an HTTP request, so a rule would be stored and
            # never consulted — worse than useless, because the editor would show
            # it and the operator would believe it did something.
            if self.rule is not None:
                raise HTTPException(
                    status_code=422,
                    detail="a webhook trigger fires on a POST, so it has no 'rule'.",
                )
            return self.kind, {}
        if self.rule is None:
            raise HTTPException(
                status_code=422, detail="a schedule trigger requires a 'rule'."
            )
        return self.kind, self.rule.to_rule().to_dict()


class ScheduleUpdate(BaseModel):
    """Every field optional; only what is present is changed.

    ``None`` and "absent" are distinguished by ``exclude_unset``, so a request that
    omits ``enabled`` does not silently disable a schedule.
    """

    name: str | None = None
    rule: RuleBody | None = None
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None
    workflow_id: str | None = None


def _wire(row: dict[str, Any]) -> dict[str, Any]:
    """Add the derived fields, so the UI computes nothing."""
    kind = row.get("kind") or store.KIND_SCHEDULE
    # A webhook has no rule, so a rule error would be a false alarm on every one of
    # them. Only a time-based row can be broken in that way.
    bad = rule_error(row.get("rule")) if kind == store.KIND_SCHEDULE else ""
    out = {
        **row,
        "rule_error": bad,
        "description": (
            describe_rule(row.get("rule"))
            if kind == store.KIND_SCHEDULE
            else "on an inbound POST"
        ),
    }
    for key in _STAMPS:
        value = row.get(key)
        out[key] = value.isoformat() if isinstance(value, datetime) else value

    if bad:
        out["validity"] = bad
    elif not row.get("enabled"):
        out["validity"] = "disabled"
    elif row.get("status") == store.SCHEDULE_ORPHANED:
        out["validity"] = row.get("status_reason") or "its workflow no longer exists"
    else:
        out["validity"] = ""

    if kind == store.KIND_WEBHOOK and row.get("token"):
        # The URL, not just the token: a caller composing it themselves is a second
        # definition of where hooks live, and it would drift the day the prefix
        # changes.
        out["hook_url"] = f"/api/hooks/{row['token']}"
    return out


def _missing(schedule_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"No schedule with id '{schedule_id}'.")


@router.get("", summary="List schedules")
async def index() -> dict[str, Any]:
    return {"items": [_wire(row) for row in store.list_schedules()]}


@router.get("/options", summary="Rule vocabularies for the editor")
async def options() -> dict[str, Any]:
    """What the trigger editor is allowed to offer.

    Served rather than duplicated in TypeScript so a new frequency or a renamed
    weekday is one edit. The lists are short and change rarely, which is exactly
    when two copies drift unnoticed.
    """
    return {
        "kinds": list(store.KINDS),
        "frequencies": list(FREQUENCIES),
        "weekdays": [
            {"value": index, "label": name} for index, name in enumerate(WEEKDAYS)
        ],
        "hours": list(range(24)),
        "minutes": [0, 15, 30, 45],
        "timezone": "UTC",
    }


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a trigger")
async def create(body: ScheduleCreate) -> dict[str, Any]:
    kind, rule = body.checked()
    row = store.create_schedule(
        workflow_id=body.workflow_id,
        rule=rule,
        kind=kind,
        name=body.name,
        inputs=body.inputs,
        enabled=body.enabled,
    )
    # Registering the job is what fills in `next_run_at` for a schedule, so it
    # happens before the response is built — a created schedule showing no next
    # fire time would look like it had not taken. A webhook registers nothing and
    # this is a no-op for it.
    get_scheduler().sync()
    return _wire(store.get_schedule(row["id"]) or row)


@router.get("/stats", summary="What the scheduler is doing")
async def stats() -> dict[str, Any]:
    """Read-only health, because the failure mode of a scheduler is silence.

    If the loop died, every schedule simply never fires and nothing else in the
    platform looks any different.
    """
    return get_scheduler().snapshot()


@router.post("/reconcile", summary="Re-check schedules against existing workflows")
async def reconcile_now() -> dict[str, Any]:
    """Run the reconciliation on demand.

    The startup pass covers the case that actually happens — a workflow deleted
    while the platform was down — but one deleted while it is *running* leaves an
    orphan until the next restart. This makes the repair available without one.
    """
    return get_scheduler().reconcile()


@router.get("/{schedule_id}", summary="One schedule")
async def show(schedule_id: str) -> dict[str, Any]:
    row = store.get_schedule(schedule_id)
    if row is None:
        raise _missing(schedule_id)
    return _wire(row)


@router.patch("/{schedule_id}", summary="Edit a schedule")
async def patch(schedule_id: str, body: ScheduleUpdate) -> dict[str, Any]:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    if body.rule is not None:
        # `model_dump` turned the `RuleBody` into a dict, and passing that on would
        # store the *unvalidated* shape — failing at fire time instead of here.
        changes["rule"] = body.rule.to_rule().to_dict()

    try:
        store.update_schedule(schedule_id, **changes)
    except store.ScheduleNotFound as exc:
        raise _missing(schedule_id) from exc

    # `sync` rather than a targeted `modify_job`: it is a full reconcile, it cannot
    # drift, and the alternative is a second path that has to agree with this one
    # about what a change means — including `enabled=false`, which is a removal.
    get_scheduler().sync()
    return _wire(store.get_schedule(schedule_id) or {})


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete")
async def remove(schedule_id: str) -> None:
    if not store.delete_schedule(schedule_id):
        raise _missing(schedule_id)
    get_scheduler().sync()


# ---------------------------------------------------------------------------
# Rotating a hook token
# ---------------------------------------------------------------------------


@router.post("/{schedule_id}/rotate", summary="Issue a new hook token")
async def rotate(schedule_id: str) -> dict[str, Any]:
    """Replace a webhook's token, and return the trigger with the new URL.

    This is the only remedy for a leaked hook URL, and without it the remedy would
    be deleting the trigger and rebuilding it — which loses the run history
    attached to that row and the inputs it was configured with.

    The old token stops working immediately, because the row no longer holds it.
    """
    row = store.get_schedule(schedule_id)
    if row is None:
        raise _missing(schedule_id)
    if row["kind"] != store.KIND_WEBHOOK:
        raise HTTPException(
            status_code=422, detail="only a webhook trigger has a token to rotate."
        )
    return _wire(store.rotate_token(schedule_id))
