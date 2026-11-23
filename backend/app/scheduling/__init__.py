"""Scheduled runs.

A schedule is a stored trigger plus a workflow id. When its moment arrives it does
exactly what ``POST /runs`` does, which is why this feature is small: starting a
run already had one implementation, and a second one would drift.

The three modules here split by what they know:

* :mod:`app.scheduling.rules` — the vocabulary a canvas author picks, translated
  to an APScheduler trigger. The arithmetic is APScheduler's.
* :mod:`app.scheduling.store` — the ``schedules`` table, and the two mutations the
  scheduler performs.
* :mod:`app.scheduling.service` — the loop (APScheduler's), started and stopped in
  the app's lifespan, plus the two things APScheduler cannot do for us: start a
  run, and notice that a workflow was deleted.

Not in ``app/runtime/``, and that placement is deliberate. The runtime package is
what gets vendored into an exported standalone agent, which is why it may not
import the database stack — a constraint the first version of this feature ran into
and which forced a layer of abstraction that existed only to satisfy it. Scheduling
never runs inside an exported agent: those have one workflow and terminate. The
platform owns it, so it lives on the platform side.
"""

from app.scheduling.service import (
    SchedulerService,
    build_scheduler,
    get_scheduler,
    set_scheduler,
)
from app.scheduling.store import (
    MISSED_POLICIES,
    SCHEDULE_ACTIVE,
    SCHEDULE_ORPHANED,
    ScheduleNotFound,
)

__all__ = [
    "MISSED_POLICIES",
    "SCHEDULE_ACTIVE",
    "SCHEDULE_ORPHANED",
    "ScheduleNotFound",
    "SchedulerService",
    "build_scheduler",
    "get_scheduler",
    "set_scheduler",
]
