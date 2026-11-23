"""The rule vocabulary, translated to an APScheduler trigger.

This module is a **translator, not an engine**. Everything that computes a fire
time is APScheduler's; this file only converts the words a canvas author picks into
the trigger kwargs APScheduler already understands, and describes a rule back as a
sentence for the UI.

Why APScheduler and not hand-written arithmetic
----------------------------------------------

Measured before deciding (see ``docs/target/reuse.md`` §1.33). The answers
``CronTrigger.get_next_fire_time`` gives are the same ones a hand-written
``dateutil.rrule`` adapter gave — inclusive at the boundary, strictly-after past
it, and stable across a DST transition because the timezone is UTC — so the
replacement is behaviour-preserving rather than a rewrite. What it removes is the
part with the edge cases: the boundary handling, the catch-up window, the "no such
fire time" case.

The project's own dependency list states the principle this follows, in the
comment beside ``langchain`` in ``requirements.txt``: *"We adopted it rather than
reimplementing the strategies."* An earlier version of this feature ignored that
and hand-wrote ~750 lines of scheduling arithmetic and loop.

Timezone: UTC, always, and it is not a default
-----------------------------------------------

Every trigger is built with ``timezone=UTC`` **explicitly**, never inherited.

That is not tidiness. Measured: modifying a job with a trigger that omits the
timezone gave a ``next_run_time`` of ``2026-09-17 08:56:01+08:00`` — the local
offset — even though the scheduler itself had been constructed with
``timezone=UTC``. A schedule that silently drifts to the server's local zone is
wrong in a way that only shows up as "it fired at the wrong hour for one person",
so the timezone is passed at every construction site rather than defaulted.

UTC also removes the DST problem at the root. APScheduler's own cron documentation
says a wall-clock rule in a DST-observing zone can fire **zero** times on the
spring-forward day and **twice** on the fall-back day, and calls that "not a bug".
A rule pinned to UTC has no transitions, so one occurrence means one instant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

UTC = timezone.utc


class ScheduleRuleError(ValueError):
    """A rule that cannot be interpreted.

    Raised while the user is looking at the form, never at fire time. An
    uninterpretable rule that was stored successfully and discovered only when its
    moment arrives is a schedule that fails invisibly, which is the failure mode
    this whole feature exists to avoid.
    """


#: Frequencies a canvas author may choose. Three, not APScheduler's full set:
#: a *workflow* that fires every second is not something anyone wants, and
#: offering it invites the mistake. `minutely` is here because it is the honest
#: choice for a health-check style schedule, and it gives the tests a rule that
#: does not require waiting a day.
FREQUENCIES = ("minutely", "hourly", "daily", "weekly", "monthly")

#: Three-letter day names, Monday first. APScheduler accepts these directly
#: (`day_of_week="mon,wed,fri"`), which is why the vocabulary can be this thin.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleRule:
    """A recurrence, as the canvas describes it.

    An interval, never a cron string. ``croniter`` is not a dependency, and
    APScheduler's own ``from_crontab`` documents a weekday numbering mismatch it
    calls a "historical mistake" (its cron treats 0 as Monday, standard crontab
    as Sunday) and cannot fix in the 3.x line. A user picking "Monday" from a list
    cannot make that mistake.

    Every field except ``frequency`` is optional, because "daily at 09:00" and
    "weekly on Mon, Wed, Fri at 09:00 and 17:00" should both be expressible
    without filling in the other's fields.
    """

    __slots__ = ("frequency", "interval", "minute", "hour", "weekday", "day")

    def __init__(
        self,
        frequency: str,
        *,
        interval: int = 1,
        minute: int | None = None,
        hour: Iterable[int] = (),
        weekday: Iterable[int] = (),
        day: int | None = None,
    ) -> None:
        if frequency not in FREQUENCIES:
            raise ScheduleRuleError(
                f"Unknown frequency '{frequency}'. "
                f"Expected one of: {', '.join(FREQUENCIES)}."
            )
        if interval < 1:
            raise ScheduleRuleError(f"interval must be at least 1, got {interval}.")
        if minute is not None and not 0 <= minute <= 59:
            raise ScheduleRuleError(f"minute must be 0-59, got {minute}.")
        self.hour: tuple[int, ...] = tuple(int(h) for h in hour)
        for value in self.hour:
            if not 0 <= value <= 23:
                raise ScheduleRuleError(f"hour must be 0-23, got {value}.")
        self.weekday: tuple[int, ...] = tuple(int(d) for d in weekday)
        for value in self.weekday:
            if not 0 <= value <= 6:
                raise ScheduleRuleError(f"weekday must be 0-6 (Mon=0), got {value}.")
        if day is not None and not 1 <= day <= 31:
            raise ScheduleRuleError(f"day must be 1-31, got {day}.")

        # The two combinations that would otherwise be accepted and then quietly
        # never fire, or fire on a different day than asked for.
        if frequency == "weekly" and day is not None:
            raise ScheduleRuleError(
                "A weekly rule picks days with 'weekday', not 'day' "
                "(day is a day-of-month, which belongs to monthly rules)."
            )
        if frequency != "monthly" and day is not None:
            raise ScheduleRuleError(
                f"'day' (day of month) only applies to monthly rules, "
                f"not '{frequency}'."
            )
        if interval > 1 and frequency == "monthly":
            raise ScheduleRuleError(
                "interval above 1 is not supported for monthly rules; "
                "list the months instead."
            )

        self.frequency = frequency
        self.interval = interval
        self.minute = minute
        self.day = day

    # -- serialisation ----------------------------------------------------
    #
    # Plain JSON, no ORM and no pydantic: a rule travels in a request body, sits
    # in a JSON column, and is rebuilt by the API and the store.

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"frequency": self.frequency, "interval": self.interval}
        if self.minute is not None:
            out["minute"] = self.minute
        if self.hour:
            out["hour"] = list(self.hour)
        if self.weekday:
            out["weekday"] = list(self.weekday)
        if self.day is not None:
            out["day"] = self.day
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ScheduleRule":
        """Rebuild a rule, turning every problem into a `ScheduleRuleError`.

        The ``TypeError``/``ValueError`` conversions are not defensive padding:
        this is called on data read back out of the database, and a hand-edited or
        downgraded row must produce a message an operator can act on rather than a
        traceback from inside APScheduler.
        """
        if not isinstance(payload, dict):
            raise ScheduleRuleError(
                f"rule must be an object, got {type(payload).__name__}."
            )
        known = {"frequency", "interval", "minute", "hour", "weekday", "day"}
        unknown = set(payload) - known
        if unknown:
            raise ScheduleRuleError(
                f"Unknown rule field(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}."
            )
        if "frequency" not in payload:
            raise ScheduleRuleError("rule is missing 'frequency'.")

        def _ints(key: str) -> tuple[int, ...]:
            value = payload.get(key) or ()
            # A bare int is accepted for the multi-valued fields because a form
            # sending `{"hour": 9}` means the same as `{"hour": [9]}`, and
            # rejecting it would be pedantry that costs a round trip.
            if isinstance(value, int):
                value = (value,)
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise ScheduleRuleError(f"'{key}' must be a list of integers.")
            try:
                return tuple(int(item) for item in value)
            except (TypeError, ValueError) as exc:
                raise ScheduleRuleError(f"'{key}' must contain integers: {exc}") from exc

        try:
            return cls(
                str(payload["frequency"]),
                interval=int(payload.get("interval", 1)),
                minute=None if payload.get("minute") is None else int(payload["minute"]),
                hour=_ints("hour"),
                weekday=_ints("weekday"),
                day=None if payload.get("day") is None else int(payload["day"]),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ScheduleRuleError):
                raise
            raise ScheduleRuleError(f"rule fields must be integers: {exc}") from exc

    # -- translation ------------------------------------------------------

    def to_trigger(self):
        """The APScheduler trigger for this rule. Always UTC — see the docstring."""
        if self.frequency == "minutely":
            return IntervalTrigger(minutes=self.interval, timezone=UTC)
        if self.frequency == "hourly":
            return IntervalTrigger(hours=self.interval, timezone=UTC)

        kwargs: dict[str, Any] = {"timezone": UTC}
        if self.minute is not None:
            kwargs["minute"] = self.minute
        if self.hour:
            kwargs["hour"] = ",".join(str(h) for h in sorted(self.hour))

        if self.frequency == "daily":
            # `hour` omitted means "every hour at :MM", which is a legitimate
            # daily-ish rule and exactly what CronTrigger does with no constraint.
            return CronTrigger(**kwargs)
        if self.frequency == "weekly":
            # Omitted weekday means every day, i.e. effectively daily. Stated here
            # rather than silently accepted so the behaviour is a decision.
            kwargs["day_of_week"] = (
                ",".join(WEEKDAYS[d] for d in sorted(self.weekday))
                if self.weekday
                else "*"
            )
            return CronTrigger(**kwargs)
        # monthly
        if self.day is not None:
            kwargs["day"] = self.day
        return CronTrigger(**kwargs)

    def describe(self) -> str:
        """One line for the UI and for logs. Display only — never parsed back."""
        if self.frequency == "minutely":
            return f"every {self.interval} minute(s) UTC"
        if self.frequency == "hourly":
            return f"every {self.interval} hour(s) UTC"

        bits = [self.frequency]
        if self.frequency == "weekly" and self.weekday:
            bits.append("on " + ", ".join(WEEKDAYS[d] for d in sorted(self.weekday)))
        if self.frequency == "monthly" and self.day is not None:
            bits.append(f"on day {self.day}")
        if self.hour:
            stamp = f"{self.minute:02d}" if self.minute is not None else "00"
            bits.append("at " + ", ".join(f"{h:02d}:{stamp}" for h in sorted(self.hour)))
        elif self.minute is not None:
            bits.append(f"at minute :{self.minute:02d}")
        bits.append("UTC")
        return " ".join(bits)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ScheduleRule({self.to_dict()})"


def rule_error(rule: dict[str, Any] | None) -> str:
    """Why a stored rule cannot be interpreted, or ``""`` when it can.

    Used by the API on the way out, so a broken row is reported on the list page
    rather than only in a log. A schedule that silently does nothing is otherwise
    indistinguishable from one whose time has not come yet.
    """
    try:
        ScheduleRule.from_dict(rule)
    except ScheduleRuleError as exc:
        return str(exc)
    return ""


def describe_rule(rule: dict[str, Any] | None) -> str:
    if rule_error(rule):
        return "unreadable rule"
    return ScheduleRule.from_dict(rule).describe()


def as_utc(value: datetime) -> datetime:
    """Interpret a naive datetime as UTC rather than rejecting it.

    Every datetime this project stores is UTC (see ``UtcDateTime`` in
    ``app/db/models.py``), so a naive one cannot be anything else, and refusing it
    would turn a cosmetic loss of the label into an error at the moment a schedule
    is due. The bug this guards against is the opposite one: mixing naive and aware
    values raises ``TypeError`` on comparison, which is how the missing-offset
    problem first showed up in this project.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
