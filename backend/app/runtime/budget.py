"""What a run is allowed to spend, and the one place that decides it is over.

Why this exists
---------------
A loop with a model call inside it spends money every pass. Nothing in the
platform used to stop it: `recursion_limit` bounds graph super-steps, which is a
coarse proxy for "how far did this go" and says nothing about tokens, and the
per-node timeout bounds one node rather than the run. So a workflow that is
merely *expensive* — not broken — could spend without limit, and the only signal
would be the bill.

Four limits, one mechanism
--------------------------
Tokens, cost, wall-clock, and steps are different questions with the same shape,
so they share one object and one :meth:`Budget.check`:

============================  ================================================
limit                         why it is not redundant
============================  ================================================
``max_tokens``                the thing that actually costs money
``max_cost``                  same, but in the unit a business cares about
``max_wall_seconds``          the run as a whole; a node timeout cannot see a
                              run that is slow because it has many nodes
``max_steps``                 finer than ``recursion_limit``, and it counts
                              node entries rather than graph super-steps
============================  ================================================

Any limit left at ``None`` (or ``0``) is simply not enforced, so a deployment
that wants no budget gets the previous behaviour exactly.

Where the check goes
--------------------
**Before and after each node.** Checking only before allows the last node to
spend everything; checking only after always overspends by one node. Neither is
wrong so much as late, and a budget that can be exceeded by an arbitrary amount
is not a budget. The check before is what stops the spend; the check after is what
stops it from getting worse when a single node is expensive on its own.

`ModelUsage` may be unknown
---------------------------
A provider that does not report usage yields ``None``, not zero — see the gateway
layer's rule that "unreported is not the same as none". Accumulating ``None`` as 0
would make the token budget **never fire** on exactly the models where it is
hardest to see the spend, so an unknown figure is recorded as unknown and the
budget reports it as such rather than pretending it is free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.settings import Settings


class BudgetExceeded(RuntimeError):
    """Raised when a run has spent more than it is allowed to.

    Carries the limit and the actual figure because the message a user sees has to
    answer "which one, how much was allowed, how much did it use" — a bare "budget
    exceeded" tells them nothing they can act on.
    """

    def __init__(self, limit_name: str, allowed: float, used: float, unit: str) -> None:
        self.limit_name = limit_name
        self.allowed = allowed
        self.used = used
        self.unit = unit
        super().__init__(
            f"Run stopped: {limit_name} limit is {allowed:g}{unit}, "
            f"and it has already used {used:g}{unit}."
        )


@dataclass
class Budget:
    """The limits for one run, plus what it has spent so far."""

    max_tokens: int | None = None
    max_cost: float | None = None
    max_wall_seconds: float | None = None
    max_steps: int | None = None

    #: What to do when a limit is reached. See `Settings.budget_exit_behavior`.
    #:
    #: The two values come from `ModelCallLimitMiddleware`'s `exit_behavior`
    #: (`"end" | "error"`), which is the same question asked in the LangChain
    #: agent loop. Our names differ because "end" would be misleading here: a
    #: stopped run **is** an error, and the choice is only about what survives.
    exit_behavior: str = "error"

    #: Accumulated usage. ``tokens_known`` exists because a run whose providers
    #: never reported usage must be distinguishable from a run that used none.
    tokens: int = 0
    tokens_known: bool = True
    cost: float = 0.0
    steps: int = 0

    #: Set on the first check, so wall-clock measures the run rather than the time
    #: since the process started.
    started: float = field(default_factory=time.perf_counter)

    #: Prices per 1M tokens, when a deployment has them. `None` disables the cost
    #: limit regardless of `max_cost`, because a cost limit with no price is not a
    #: limit — it would silently never fire.
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    # ------------------------------------------------------------- constructors
    @classmethod
    def from_settings(cls, settings: Settings) -> "Budget":
        return cls(
            max_tokens=_or_none(settings.max_run_tokens),
            max_cost=_or_none(settings.max_run_cost),
            max_wall_seconds=_or_none(settings.max_run_seconds),
            max_steps=_or_none(settings.max_run_steps),
            exit_behavior=settings.budget_exit_behavior or "error",
            input_price_per_million=_or_none(settings.input_price_per_million),
            output_price_per_million=_or_none(settings.output_price_per_million),
        )

    @property
    def keeps_partial_result(self) -> bool:
        """Whether a stopped run should report what it had produced."""
        return self.exit_behavior == "keep"

    @property
    def enabled(self) -> bool:
        """Whether any limit is configured. Lets callers skip the bookkeeping."""
        return any(
            value is not None
            for value in (self.max_tokens, self.max_cost, self.max_wall_seconds, self.max_steps)
        )

    # ---------------------------------------------------------------- accounting
    def add_usage(self, usage: Any) -> None:
        """Record one model call's usage.

        Accepts the gateway's ``ModelUsage`` (or anything shaped like it) and
        tolerates ``None``. An unknown usage marks the token figure as incomplete
        instead of adding zero — see the module docstring.
        """
        if usage is None:
            self.tokens_known = False
            return

        prompt = _int_or_none(_get(usage, "prompt_tokens"))
        completion = _int_or_none(_get(usage, "completion_tokens"))
        total = _int_or_none(_get(usage, "total_tokens"))

        if total is None:
            if prompt is None and completion is None:
                self.tokens_known = False
                return
            total = (prompt or 0) + (completion or 0)

        self.tokens += total

        if self.input_price_per_million is not None or self.output_price_per_million is not None:
            self.cost += (prompt or 0) / 1_000_000 * (self.input_price_per_million or 0)
            self.cost += (completion or 0) / 1_000_000 * (self.output_price_per_million or 0)

    def add_step(self) -> None:
        self.steps += 1

    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started

    # ------------------------------------------------------------------ checking
    def check(self) -> None:
        """Raise :class:`BudgetExceeded` if any limit is passed.

        Ordered cheapest-first so the reported reason is the most specific one
        available: steps and wall-clock are exact, tokens are exact when reported,
        and cost is derived from optional prices.
        """
        if self.max_steps is not None and self.steps > self.max_steps:
            raise BudgetExceeded("step", self.max_steps, self.steps, " steps")

        if self.max_wall_seconds is not None:
            elapsed = self.elapsed_seconds()
            if elapsed > self.max_wall_seconds:
                raise BudgetExceeded(
                    "wall-clock", self.max_wall_seconds, round(elapsed, 2), "s"
                )

        if (
            self.max_tokens is not None
            and self.tokens_known
            and self.tokens > self.max_tokens
        ):
            raise BudgetExceeded("token", self.max_tokens, self.tokens, " tokens")

        if self.max_cost is not None and self.cost > self.max_cost:
            raise BudgetExceeded("cost", self.max_cost, round(self.cost, 6), "")

    # ----------------------------------------------------------------- reporting
    def usage(self) -> dict[str, Any]:
        """The spent-so-far figures, for the run summary and the UI."""
        payload: dict[str, Any] = {
            "tokens": self.tokens,
            # Explicit, so a client can say "unreported" rather than showing 0 for
            # a model that simply did not tell us.
            "tokens_known": self.tokens_known,
            "steps": self.steps,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
        }
        if self.max_cost is not None:
            payload["cost"] = round(self.cost, 6)
        for key, value in (
            ("max_tokens", self.max_tokens),
            ("max_cost", self.max_cost),
            ("max_wall_seconds", self.max_wall_seconds),
            ("max_steps", self.max_steps),
        ):
            if value is not None:
                payload[key] = value
        return payload


def _get(obj: Any, name: str) -> Any:
    """Read a field from an object or a dict; ``None`` when absent."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _or_none(value: Any) -> Any:
    """Treat ``0`` / ``""`` as "not configured" rather than as a limit of zero.

    A limit of zero would stop every run at the first check, which is never what
    someone means by leaving a field empty.
    """
    if value in (0, 0.0, "", None):
        return None
    return value


# ---------------------------------------------------------------------------
# The run's budget, reachable from inside a node
# ---------------------------------------------------------------------------
#
# The compiler's per-node check needs the *same* object the manager accounts
# into, and the config LangGraph hands a node is not a safe place to carry it:
# `configurable` may be copied per task, and a copied budget would silently start
# every node from zero — a budget that never fires, which is the worst kind.
#
# A keyed registry avoids that entirely, and mirrors how the checkpointer
# provider is process-scoped. Entries are removed when the run ends, so this
# cannot grow with run count.
_ACTIVE: dict[str, "Budget"] = {}


def register_budget(run_id: str, budget: "Budget") -> None:
    _ACTIVE[run_id] = budget


def get_budget(run_id: str | None) -> "Budget" | None:
    """The budget for a run, or ``None`` when the run has none (tests, nodes
    invoked outside a run).

    ``None`` means "no limits configured", not "no budget" — callers skip the
    check rather than failing.
    """
    if run_id is None:
        return None
    return _ACTIVE.get(run_id)


def clear_budget(run_id: str) -> None:
    _ACTIVE.pop(run_id, None)


__all__ = [
    "Budget",
    "BudgetExceeded",
    "clear_budget",
    "get_budget",
    "register_budget",
]
