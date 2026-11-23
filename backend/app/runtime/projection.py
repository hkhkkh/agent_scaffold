"""Stopping a run *before* it overruns, rather than after.

The gap this closes
-------------------
`budget.py` answers "has this run already gone too far" and does it by raising —
a **reactive** stop, and the right thing for a hard ceiling. It cannot answer the
question that actually keeps a long-running agent useful:

    "there are 3 steps left in the allowance and 8 tasks on the plan — what
     should this run do about that?"

Raising is one answer. Finishing with what it has is often a better one, and it
has to be decided *while there is still allowance left to decide with*. So this
module adds two things to the budget: a **forecast** (what is left, in the units
the run is actually spending) and a **policy** (what to do about it).

What is reused
--------------
LangGraph's `RemainingSteps` is the same idea one level down — it is a managed
state value that tracks super-steps left before `GraphRecursionError`. Measured:
it exists at `langgraph.managed.RemainingSteps` and works. It is **not** used here
because the unit is wrong for us: a super-step is one graph pass, and one of our
nodes is one super-step regardless of whether it spent 10 tokens or 100,000. A
run needs to be stopped on **tokens, cost, seconds and steps**, which is what
`Budget` already counts. So the shape is borrowed and the counting is ours.

`RunControl` / `GraphDrained` (see `docs/target/reuse.md` §1.29) is the other
half, and it is a different operation: it stops a run because the *process* is
shutting down, resumably. That belongs in the supervisor, not here.

Why this is not a circuit breaker
---------------------------------
The roadmap calls task 23 "预算护栏与熔断". A circuit breaker in the usual sense —
`pybreaker`'s — guards a **shared dependency**: it trips on failures, stays open
across callers, and half-opens to probe. Measured: neither `pybreaker` nor
`aiobreaker` is installed, and the semantics do not transfer anyway. What a run
needs is a **spend** limit: per-run, monotonic, no probing, and the useful action
is to stop gracefully rather than to refuse future calls.

So the name kept is "breaker" only in the sense of "it stops", and the mechanism
is a forecast plus a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: What to do when the forecast says the run will not finish inside its budget.
#:
#: `warn` — say so and keep going. The reactive ceiling in `budget.py` still
#: applies, so this is "notify, do not interfere". The default, because a
#: forecast is wrong whenever the rest of the run is cheaper than the start.
WIND_DOWN_WARN = "warn"
#: `stop` — finish the current node and then end the run, keeping its output.
#: This is the "return what you have" behaviour, and the reason the forecast
#: exists: stopping *after* the limit is exceeded cannot keep anything.
WIND_DOWN_STOP = "stop"

WIND_DOWN_MODES = (WIND_DOWN_WARN, WIND_DOWN_STOP)


class WindDown(RuntimeError):
    """A run stopped early on purpose, with allowance still left.

    Deliberately **not** a :class:`~app.runtime.budget.BudgetExceeded`, and the
    difference is the whole point: an exceeded budget means the run went past a
    ceiling and did not finish what it was asked to do, while a wind-down means
    the run was **steered to a controlled stop**. Reporting the second as the
    first would tell a caller their run failed when it was in fact managed, and
    would make `budget_exit_behavior` decide how a *voluntary* stop behaves.

    A separate type also keeps the reason in the message: `reasons` is the
    forecast's own words, not a bare "stopped".
    """

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(
            "Run stopped early to stay inside its budget: " + "; ".join(reasons)
        )

#: How many steps ahead the forecast looks. Small on purpose: the further ahead,
#: the more it is extrapolation rather than measurement.
FORECAST_STEPS = 3

#: Below this fraction of a limit, a run is not warned about. A forecast that
#: fires at 51% used would warn constantly and be ignored.
DEFAULT_HEADROOM = 0.2

#: Slack for the step-count comparison, so a boundary that lands on a whole
#: number is not missed by binary floating point. See the forecast below.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Projection:
    """What is left in each budget, and whether the run should wind down.

    One object rather than four numbers because the *decision* is what callers
    act on, and a caller that has to combine four figures will combine them
    differently from the next caller.
    """

    #: Fraction of each limit consumed, 0..1+. Empty when that limit is unset.
    used_fraction: dict[str, float]
    #: The single limit closest to being exhausted, or `None` when none is set.
    tightest: str | None
    #: How much of the tightest limit is left, as a fraction.
    remaining_fraction: float
    #: Whether the run is projected to overrun before it finishes.
    should_wind_down: bool
    #: Human-readable reasons, for the log line. Empty when nothing is tight.
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "used_fraction": {k: round(v, 4) for k, v in self.used_fraction.items()},
            "tightest": self.tightest,
            "remaining_fraction": round(self.remaining_fraction, 4),
            "should_wind_down": self.should_wind_down,
            "reasons": list(self.reasons),
        }


def project(
    budget: Any,
    *,
    headroom: float = DEFAULT_HEADROOM,
    steps_ahead: int = FORECAST_STEPS,
) -> Projection:
    """Forecast whether `budget` will be exhausted before the run finishes.

    The projection is deliberately simple: it asks whether the *average* cost per
    step so far, extrapolated `steps_ahead` further, would cross a limit, and
    whether the existing headroom is already thin. Both are cheap, and both are
    honest about being estimates — the docstring says so rather than implying a
    prediction.

    A budget with no limits configured projects nothing, which is what makes this
    free to call on every node.
    """
    if budget is None or not budget.enabled:
        return Projection({}, None, 1.0, False, ())

    elapsed = budget.elapsed_seconds()
    checks: list[tuple[str, float, float]] = []  # (name, used, allowed)

    for name, used, allowed in (
        ("steps", float(budget.steps), budget.max_steps),
        ("wall-clock", elapsed, budget.max_wall_seconds),
        ("tokens", float(budget.tokens), budget.max_tokens if budget.tokens_known else None),
        ("cost", float(budget.cost), budget.max_cost),
    ):
        if allowed:
            checks.append((name, used, float(allowed)))

    if not checks:
        return Projection({}, None, 1.0, False, ())

    fractions = {name: (used / allowed if allowed else 0.0) for name, used, allowed in checks}
    tightest, _, allowed = max(checks, key=lambda item: fractions[item[0]])
    remaining = max(0.0, 1.0 - fractions[tightest])

    reasons: list[str] = []

    # 1. Already thin: less than the headroom fraction left.
    if remaining <= headroom:
        reasons.append(
            f"{tightest} is {fractions[tightest] * 100:.0f}% used "
            f"({remaining * 100:.0f}% left)"
        )

    # 2. Forecast: how many more steps of this size fit in what is left?
    #
    #    Expressed as *steps remaining* rather than as a projected percentage, and
    #    that is a correction. The first version computed
    #    `fraction + per_step * steps_ahead > 1.0`, which is the same arithmetic
    #    with a worse boundary: at 7/10 steps used with 3 steps of lookahead it
    #    gives exactly 1.0, so `>` missed a run that had 3 steps of budget left
    #    and 3 steps to go. Written as a count, the comparison is `<=`, which is
    #    the question a reader actually asks.
    #
    #    Only for limits that grow per step. Wall-clock grows with time, not with
    #    step count, so extrapolating it by steps would be wrong in exactly the
    #    case it matters — one slow node.
    if budget.steps > 0 and steps_ahead > 0 and tightest in ("steps", "tokens", "cost"):
        per_step = fractions[tightest] / budget.steps
        if per_step > 0:
            steps_left = remaining / per_step
            # `+ _EPSILON` because the boundary case is *exactly* the one that
            # matters and floating point puts it on the wrong side.
            #
            # Measured: 7 steps used of 10 gives `remaining = 1.0 - 0.7 =
            # 0.30000000000000004`, so three steps of budget left comes out as
            # `3.0000000000000004` and a bare `<= 3` misses it — a run with
            # exactly enough budget warns never and stops at the ceiling instead,
            # which is the behaviour this feature exists to improve on.
            if steps_left <= steps_ahead + _EPSILON:
                # Phrased so no verb has to agree with a count, and so `steps`
                # never appears as both the unit and the quantity. Three drafts
                # got one of those wrong; this one cannot.
                unit = "steps" if tightest == "steps" else f"steps of {tightest}"
                reasons.append(
                    f"the remaining {unit} run out at step "
                    f"{budget.steps + steps_left:.0f}; {steps_ahead} ahead is planned"
                )

    return Projection(
        used_fraction=fractions,
        tightest=tightest,
        remaining_fraction=remaining,
        should_wind_down=bool(reasons),
        reasons=tuple(reasons),
    )


def wind_down_mode(settings: Any) -> str:
    """The configured mode, defaulting to `warn` on anything unrecognised.

    Warn is the safe direction: `stop` ends a run, and doing that because of a
    typo in an optional field would lose work that the reactive ceiling would
    have allowed.
    """
    raw = str(getattr(settings, "budget_wind_down", "") or WIND_DOWN_WARN).strip().lower()
    return raw if raw in WIND_DOWN_MODES else WIND_DOWN_WARN


__all__ = [
    "DEFAULT_HEADROOM",
    "FORECAST_STEPS",
    "WIND_DOWN_MODES",
    "WIND_DOWN_STOP",
    "WIND_DOWN_WARN",
    "Projection",
    "WindDown",
    "project",
    "wind_down_mode",
]
