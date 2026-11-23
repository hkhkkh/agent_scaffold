"""The scheduler: APScheduler's loop, driven by rows in our table.

What is ours and what is theirs
--------------------------------

**Theirs:** deciding when a rule fires next, waiting for that moment, and running
the job. That is ``AsyncIOScheduler`` plus a trigger from
``app/scheduling/rules.py``. None of it is written here.

**Ours:** two things APScheduler cannot know about.

1. **Starting the run.** A scheduled run is not a special kind of run — the job
   function calls ``RunManager.create`` then ``RunManager.start``, the same two
   calls ``POST /runs`` makes. So it queues behind the same concurrency limit, is
   bounded by the same budget, gets the same fault isolation, and appears in the
   same run history.
2. **Reconciliation.** APScheduler has no concept of "the workflow this job points
   at no longer exists", so the platform tells it: see ``reconcile``.

Why `MemoryJobStore` and a re-registration at startup
------------------------------------------------------

The rows in our table are the source of truth; APScheduler holds the schedule in
memory. At startup every live schedule is registered again, so a restart costs
nothing — the rule is in the row, and the next fire time is derived from it.

The alternative, ``SQLAlchemyJobStore``, would put APScheduler's own table in the
schema, outside alembic. This project runs ``upgrade head`` at startup and has a
test asserting the migrated schema matches the models, so an unmanaged second table
is a real cost. It would also require the job function to be picklable by import
path, which is a constraint on how the platform is wired for no gain here.

What APScheduler replaces, measured
------------------------------------

An earlier version of this feature hand-wrote a ``dateutil.rrule`` adapter for the
arithmetic and its own ``while`` loop with a clamping sleep. Both are gone.
``misfire_grace_time`` is the lateness tolerance that was hand-written;
``coalesce=True`` is the "do not run the backlog" policy that was hand-written;
``max_instances=1`` is the overlap guard; ``pause_job``/``resume_job`` is the
enabled flag. See ``docs/target/reuse.md`` §1.33 for the measurements.

The one trap, measured
----------------------

**Every trigger must be constructed with ``timezone=UTC`` explicitly.** Modifying a
job with a trigger that omits it produced a ``next_run_time`` carrying the server's
local offset (``+08:00``) even though the scheduler itself was built with
``timezone=UTC``. A schedule that silently drifts to the server's local zone is
wrong in a way that surfaces as "it fired at the wrong hour for one person", so
``rules.ScheduleRule.to_trigger`` passes it at every construction site.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduling.rules import ScheduleRule, ScheduleRuleError, as_utc
from app.scheduling import store

logger = logging.getLogger(__name__)

UTC = timezone.utc

#: How late a fire may be before APScheduler refuses to run it.
#:
#: This is the number APScheduler calls ``misfire_grace_time``, and it exists
#: because a job whose time passed while the process was down is otherwise either
#: run immediately or dropped, with no policy. Set generously: a run that starts
#: ten minutes late is almost always better than a run that does not happen, and
#: the *recording* of lateness is done by :mod:`app.scheduling.store`, not by
#: refusing the run.
MISFIRE_GRACE_SECONDS = 3600


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return as_utc(value).isoformat() if isinstance(value, datetime) else str(value)


class SchedulerService:
    """Owns the APScheduler instance and the lifecycle the app needs.

    Not a module-level singleton: the process-wide accessor is
    ``get_scheduler()``/``set_scheduler()`` at the bottom, matching how the queue,
    run store, and checkpointer providers are wired, so tests can install their own
    instance and never touch the real one.

    **This is the first long-lived background task in this project**, which is why
    the lifecycle is explicit. A task that outlives its test leaks into the next
    test's event loop and produces failures that make no sense — the project
    already hit the analogous problem with the checkpointer provider (62 tests
    failing for a reason unrelated to what they tested). ``stop()`` is therefore
    awaited, and ``start()`` is idempotent.
    """

    def __init__(self, *, enabled: bool = True, missed_policy: str = "skip") -> None:
        self.enabled = enabled
        self.missed_policy = missed_policy if missed_policy in store.MISSED_POLICIES else "skip"
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()}, timezone=UTC
        )
        self._started = False
        #: Counters so "is the scheduler working" is answerable without reading
        #: stdout. The failure mode of a scheduler is silence: if the loop died,
        #: every schedule simply never fires and nothing else looks different.
        self.started_count = 0
        self.failed_count = 0
        self.missed_count = 0
        self.last_error = ""

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start APScheduler and register every live schedule. Idempotent."""
        if self._started or not self.enabled:
            return
        self._scheduler.start()
        self._started = True
        self.sync()

    async def stop(self) -> None:
        """Stop the loop. Awaited, so a caller can rely on it having stopped.

        ``wait=False`` because the jobs are ``async`` and APScheduler runs them on
        this loop: waiting would mean blocking the loop we are shutting down, and
        an in-flight run is already handled elsewhere — ``RunManager.shutdown``
        cancels in-flight runs, which is the thing that actually owns them.
        """
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False

    @property
    def running(self) -> bool:
        return self._started and self._scheduler.running

    # -- registration -----------------------------------------------------

    def sync(self) -> None:
        """Make APScheduler's jobs match the live rows. Called at startup and after edits.

        A full reconcile rather than per-schedule add/remove: it is a handful of
        rows, it cannot drift, and the alternative is a second code path that has
        to agree with this one about what "live" means.
        """
        if not self._started:
            return
        wanted = {row["id"]: row for row in store.live_schedules()}
        for job in self._scheduler.get_jobs():
            if job.id not in wanted:
                self._scheduler.remove_job(job.id)

        for schedule_id, row in wanted.items():
            try:
                rule = ScheduleRule.from_dict(row["rule"])
            except ScheduleRuleError as exc:
                # Reported on the row, and no job registered. Not raised: one
                # broken schedule must not stop the others being registered.
                store.record_error(schedule_id, str(exc))
                continue
            self._scheduler.add_job(
                self._fire,
                trigger=rule.to_trigger(),
                id=schedule_id,
                kwargs={"schedule_id": schedule_id},
                # See the module docstring for what each of these replaces.
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
                coalesce=self.missed_policy == "skip",
                max_instances=1,
                replace_existing=True,
            )
            self._remember_next_run(schedule_id)

    def _remember_next_run(self, schedule_id: str) -> None:
        """Copy APScheduler's decision onto the row.

        Read from the job rather than recomputed so the list page shows the time
        the loop will actually act on. Two computations of "when next" is how a UI
        ends up displaying a time that never arrives.
        """
        job = self._scheduler.get_job(schedule_id)
        store.set_next_run(schedule_id, None if job is None else job.next_run_time)

    def reconcile(self) -> dict[str, int]:
        """Retire schedules whose workflow is gone, then re-sync.

        Runs at startup and on demand. The startup pass covers the case that
        actually happens — a workflow deleted while the platform was down — and the
        on-demand call covers one deleted while the platform is running.
        """
        from app.db.repository import list_workflows

        result = store.reconcile({row["id"] for row in list_workflows()})
        if result["orphaned"] or result["revived"]:
            logger.warning(
                "Schedules reconciled: %d orphaned, %d revived",
                result["orphaned"],
                result["revived"],
            )
        self.sync()
        return result

    # -- the job ----------------------------------------------------------

    async def _fire(self, schedule_id: str) -> None:
        """Start a run for this schedule. Never raises.

        APScheduler calls this at the trigger's moment. An exception here would be
        logged by APScheduler and then the schedule would keep firing, so failures
        are recorded on the row — where an operator will see them — and swallowed.

        The order matters. ``mark_fired`` is written *before* the run is attempted,
        so a crash during startup leaves evidence that this moment was reached. The
        alternative ordering loses the fact that the fire happened at all.
        """
        row = store.get_schedule(schedule_id)
        if row is None:
            # Deleted between the trigger firing and here. Drop the job quietly.
            self._forget(schedule_id)
            return

        intended = as_utc(row["next_run_at"] or datetime.now(UTC))
        store.mark_fired(schedule_id, intended=intended)

        # How late this fire is, against the grace APScheduler applied. Recorded
        # rather than acted on: the runs still happen, but a schedule that is
        # consistently late is a fact worth surfacing.
        lateness = (datetime.now(UTC) - intended).total_seconds()
        if lateness > MISFIRE_GRACE_SECONDS:
            store.note_missed(schedule_id, 1)
            self.missed_count += 1
            logger.warning(
                "Schedule %s fired %.0fs late (grace %ds); missed run recorded",
                schedule_id,
                lateness,
                MISFIRE_GRACE_SECONDS,
            )

        try:
            run_id = await self._start_run(row)
        except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
            text = f"{type(exc).__name__}: {exc}"
            store.record_error(schedule_id, text)
            self.failed_count += 1
            self.last_error = text
            logger.warning("Schedule %s could not start a run: %s", schedule_id, text)
        else:
            store.record_started(schedule_id, run_id)
            self.started_count += 1
            logger.info("Schedule %s started run %s", schedule_id, run_id)

        self._remember_next_run(schedule_id)

    async def _start_run(self, row: dict[str, Any], extra_inputs: dict[str, Any] | None = None) -> str:
        """Create and start a run — the same two calls the run endpoint makes.

        The single place a trigger turns into a run, shared by both kinds: the
        timer below calls it with the schedule's stored inputs, the webhook calls
        it with the request body. One implementation is the point — a second copy
        is how the two would come to disagree about whether a run is subject to
        the concurrency limit, the budget, and the run history (it is, and it must
        keep being, because that is what makes a triggered run an ordinary run).

        The imports are local so this module stays importable in a context without
        a database, which is what keeps the rule vocabulary testable on its own.
        """
        from app.db.repository import WorkflowNotFound, get_workflow
        from app.graph.schema import RunCreateRequest
        from app.runtime.manager import RunError, get_run_manager

        workflow_id = row["workflow_id"]
        try:
            graph = get_workflow(workflow_id)
        except WorkflowNotFound as exc:
            # The repair is `reconcile()`, not a retry — this will fail identically
            # on every fire until someone re-points the trigger.
            raise RuntimeError(f"workflow '{workflow_id}' no longer exists") from exc

        # Stored inputs first, request body on top. The stored ones are the
        # trigger's defaults ("this schedule always runs with these"); the body is
        # the caller's specifics. Letting the body win means a webhook can pass
        # values without the trigger row having to be edited per call, and the
        # reverse order would make a payload key silently ignored.
        inputs = {**dict(row.get("inputs") or {}), **(extra_inputs or {})}
        manager = get_run_manager()
        request = RunCreateRequest(workflow_id=workflow_id, inputs=inputs)
        try:
            context = await manager.create(workflow_id, graph, request)
        except RunError as exc:
            raise RuntimeError(str(exc)) from exc
        await manager.start(context)
        return context.id

    async def fire_now(
        self, row: dict[str, Any], *, inputs: dict[str, Any] | None = None
    ) -> str:
        """Start a run for a trigger immediately, off the clock.

        This is what a webhook calls. It deliberately does **not** go through
        APScheduler: there is no moment to schedule, and routing an inbound
        request through a job store would mean persisting and re-reading an event
        that is happening right now.

        The reliability bookkeeping is the same as the timer's, for the same
        reason — a webhook that fails has to leave a trace somewhere a person will
        look, and the trigger's own row is that place.
        """
        schedule_id = row["id"]
        store.mark_fired(schedule_id, intended=datetime.now(UTC))
        try:
            run_id = await self._start_run(row, inputs)
        except Exception as exc:  # noqa: BLE001 - recorded on the row, then re-raised
            text = f"{type(exc).__name__}: {exc}"
            store.record_error(schedule_id, text)
            self.failed_count += 1
            self.last_error = text
            # Re-raised, unlike the timer path: a webhook has a caller waiting, and
            # answering 2xx for a run that never started would be a lie the caller
            # cannot detect.
            raise
        store.record_started(schedule_id, run_id)
        self.started_count += 1
        logger.info("Webhook %s started run %s", schedule_id, run_id)
        return run_id

    def _forget(self, schedule_id: str) -> None:
        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:  # noqa: BLE001 - already gone is the desired state
            pass

    # -- what the API reports ---------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """What the loop is doing, for ``GET /schedules/stats``."""
        jobs = self._scheduler.get_jobs() if self._started else []
        return {
            "enabled": self.enabled,
            "running": self.running,
            "jobs": len(jobs),
            "next_run_at": _iso(
                min((j.next_run_time for j in jobs if j.next_run_time), default=None)
            ),
            "missed_policy": self.missed_policy,
            "misfire_grace_seconds": MISFIRE_GRACE_SECONDS,
            "started": self.started_count,
            "failed": self.failed_count,
            "missed": self.missed_count,
            "last_error": self.last_error,
        }


def build_scheduler(settings: Any = None) -> SchedulerService:
    """Build from settings. Eager, in the lifespan, like every other provider.

    Eagerly rather than lazily for the same reason as the queue and the run store:
    a misspelled ``MISSED_RUN_POLICY`` should be visible in the startup log, not
    discovered when a schedule is due. An invalid value falls back to the default
    with a warning rather than stopping the process — scheduling is not
    load-bearing for the rest of the platform, and refusing to boot over it would
    be the wrong trade.
    """
    if settings is None:
        from app.settings import get_settings

        settings = get_settings()

    policy = getattr(settings, "missed_run_policy", "skip")
    if policy not in store.MISSED_POLICIES:
        logger.warning(
            "MISSED_RUN_POLICY=%r is not one of %s; using 'skip'.",
            policy,
            ", ".join(store.MISSED_POLICIES),
        )
        policy = "skip"

    return SchedulerService(
        enabled=getattr(settings, "scheduler_enabled", True), missed_policy=policy
    )


#: The process-wide service. ``None`` until built; see `get_scheduler`.
_SCHEDULER: SchedulerService | None = None


def get_scheduler() -> SchedulerService:
    """The process-wide scheduler, building one on first use.

    Used by the API so ``/schedules/stats`` works even in a test that never entered
    the lifespan. The counters then read as "never ticked", which is true.
    """
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = build_scheduler()
    return _SCHEDULER


def set_scheduler(scheduler: SchedulerService | None) -> None:
    """Install one, or ``None`` to forget it. The seam tests use."""
    global _SCHEDULER
    _SCHEDULER = scheduler


__all__ = [
    "MISFIRE_GRACE_SECONDS",
    "SchedulerService",
    "build_scheduler",
    "get_scheduler",
    "set_scheduler",
]
