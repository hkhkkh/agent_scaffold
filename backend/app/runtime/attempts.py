"""Per-node execution policy: how long one attempt may take, and how many tries.

Why this lives here and not in each node
----------------------------------------
Every node is invoked from exactly one place — ``graph/compiler.py``'s
``_make_node_fn`` — so a policy applied at that seam covers all twelve node
types without editing a single ``nodes/*.py``.

Why a timeout is on by default and a retry is not
-------------------------------------------------
A timeout is **throttling**: it cannot change the outcome of a graph that
finishes inside its budget. A retry is **repeated side effects**: a node that
charged a card, sent an invoice, or inserted a row will happily do it again. The
two therefore get opposite defaults, and only retries have to be asked for.

Why the deadline is the budget *plus a grace period*
----------------------------------------------------
Some nodes enforce a deadline of their own and can say something specific about
it — the Code node's sandbox kills the child process and reports why, while this
layer can only report that time ran out. If both used the same number, which one
fired would be a race, and roughly half of all timeouts would surface as the
vague message. So the executor waits longer on purpose and lets the specific
report win. The message it produces still quotes the budget the author set,
never the padded deadline.

Not a swappable capability
--------------------------
The capability layer's ``ABC`` + ``get_*``/``set_*`` shape earns its keep there
because those seams have more than one implementation (a local runner, a
container runner). There is one implementation of this policy, so an injection
point would be ceremony. Tests build an :class:`AttemptPolicy` and pass it in,
which needs no global.

Why `on_error` is ours when LangGraph has `error_handler`
---------------------------------------------------------
It is not that the framework cannot do it. Measured, twice, because the first
answer was wrong: `error_handler=` **can** receive the exception
(``def handler(state, error: NodeError)``, injected by annotation) and **can**
route to the failed node's descendants (return ``Command(goto=...)``).

What it cannot be is **data**. It takes a Python function, so the handler has to
exist before we know which nodes will use one — and our graph is a JSON document
whose nodes are not known until run time. `on_error` is a string in a node's
config precisely so the author picks it in the Inspector.

So this is the second case in `docs/target/reuse.md` §0 — upstream has it, we
judged it unsuitable, and the reason is written down — not the third.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Extra time the executor waits beyond the advertised budget — see the module
#: docstring. This is what lets a node's own, more specific deadline report first.
GRACE_SECONDS = 5.0

#: Ceiling on ``max_retries``. A mistyped value ("20" where "2" was meant) turns
#: one failing workflow into a long one that pays a model on every attempt, so
#: the value is clamped and the fact is logged rather than honoured.
MAX_RETRIES_CEILING = 5

#: Keys read out of the merged node config (``compiler._merge_tweaks`` output).
#: Deliberately not ``timeout`` / ``max_retries``: an LLM node's config already
#: uses those two names for the provider's HTTP client (see
#: ``runtime/ai/provider.py``'s ``_CONFIG_KEYS``), and one key cannot mean two things in
#: the same dictionary.
KEY_TIMEOUT = "node_timeout_seconds"
KEY_MAX_RETRIES = "node_max_retries"
#: What happens when a node fails *after* its retries are spent. Deliberately a
#: separate key from the retry count: "how many times do I try" and "what do I do
#: when trying is over" are two decisions, and a deployment that wants one of
#: them rarely wants the other changed.
KEY_ON_ERROR = "on_error"

#: `on_error` values.
#:
#: `stop` — the run fails and says which node failed. The default, because it is
#: what the runtime did before this option existed, and because a silently
#: skipped node is how a workflow appears to work while returning wrong answers.
ON_ERROR_STOP = "stop"
#: `continue` — the node stores its failure in `vars` and the run carries on.
#: This is the "one bad row does not fail the batch" case, and the reason it is a
#: parameter rather than a global setting: only the author knows which nodes are
#: allowed to fail.
ON_ERROR_CONTINUE = "continue"
#: `default` — the same as `continue`, except the node also gets the value from
#: `on_error_value` (default `None`), so a downstream node sees a usable value
#: rather than a marker it has to know how to interpret.
ON_ERROR_DEFAULT = "default"
ON_ERROR_MODES = (ON_ERROR_STOP, ON_ERROR_CONTINUE, ON_ERROR_DEFAULT)

#: Where a continued failure is recorded, so `{{ vars.<node>.error }}` reads it
#: and the run timeline can point at it.
ERROR_KEY = "__error__"


class NodeTimeoutError(Exception):
    """A node attempt ran past its time budget.

    Deliberately **not** a subclass of the builtin :class:`TimeoutError`, which
    is an :class:`OSError` — and the runtime wraps subprocess and socket waits in
    ``except OSError`` in several places. A node taking too long is a scheduling
    fact, not an operating-system failure, and it must not be swallowed by error
    handling that was written for file descriptors.
    """


@dataclass(frozen=True)
class AttemptPolicy:
    """How one node gets executed: the per-attempt budget, and the retry count."""

    timeout_seconds: float | None = None
    """Budget for a **single attempt**. ``None`` means no limit at all."""

    max_retries: int = 0
    """Extra attempts after the first. ``0`` means the node runs exactly once."""

    on_error: str = ON_ERROR_STOP
    """What happens when the attempts are spent and the node still failed.

    One of :data:`ON_ERROR_MODES`. Only :data:`ON_ERROR_STOP` can fail a run; the
    other two let the graph continue, which is what makes partial-batch work
    possible.
    """

    on_error_value: Any = None
    """The value a `default` node writes when it fails.

    Carried on the policy rather than read again at the catch site so that the
    decision and the value it falls back to cannot be resolved from different
    places.
    """

    grace_seconds: float = GRACE_SECONDS
    """How much longer than ``timeout_seconds`` the executor actually waits."""

    @property
    def continues_on_error(self) -> bool:
        """Whether a spent node is survivable. The negation is used more often."""
        return self.on_error in (ON_ERROR_CONTINUE, ON_ERROR_DEFAULT)

    @property
    def failure_value(self) -> Any:
        """What a `default` node's output handle holds after it failed."""
        return self.on_error_value if self.on_error == ON_ERROR_DEFAULT else None

    @property
    def deadline_seconds(self) -> float | None:
        """The deadline the executor enforces, including the grace period."""
        if self.timeout_seconds is None:
            return None
        return self.timeout_seconds + max(0.0, self.grace_seconds)

    @property
    def attempts_allowed(self) -> int:
        """Total executions of the node, first attempt included."""
        return self.max_retries + 1

    @classmethod
    def resolve(
        cls,
        config: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> AttemptPolicy:
        """Build the policy for a node from its merged config and the deployment.

        ``config`` is ``compiler._merge_tweaks``'s output, so it already folds
        (deployment default <- per-run tweaks <- the node's stored config). That
        is why no schema field is involved: an existing workflow can carry an
        override today, and no migration is needed to add one.

        The config key names match the setting names exactly — one concept, one
        name — and are distinct from the LLM client's ``timeout``/``max_retries``.
        """
        config = config if isinstance(config, dict) else {}
        settings = settings or get_settings()

        raw_timeout = config.get(KEY_TIMEOUT)
        if raw_timeout in (None, ""):
            raw_timeout = settings.node_timeout_seconds
        budget = _as_float(raw_timeout, settings.node_timeout_seconds, KEY_TIMEOUT)

        raw_retries = config.get(KEY_MAX_RETRIES)
        if raw_retries in (None, ""):
            raw_retries = settings.node_max_retries
        retries = _as_int(raw_retries, 0, KEY_MAX_RETRIES)

        # An unrecognised mode becomes `stop` rather than raising. This is the
        # one place in the policy where a bad value is accepted: `stop` is the
        # safe direction (a run fails loudly instead of quietly skipping work),
        # and refusing to start a run over a typo in an optional field would be
        # the more disruptive failure.
        raw_on_error = str(config.get(KEY_ON_ERROR) or ON_ERROR_STOP).strip().lower()
        on_error = raw_on_error if raw_on_error in ON_ERROR_MODES else ON_ERROR_STOP

        # A budget of 0 (or a negative one) means "no limit". Keeping "off" a
        # falsy number rather than `None` is what lets the setting stay a float,
        # which is all an environment variable can carry.
        return cls(
            timeout_seconds=budget if budget > 0 else None,
            max_retries=retries,
            on_error=on_error,
            on_error_value=config.get("on_error_value"),
            # Read at call time rather than left to the dataclass default, so the
            # grace period is a single knob a test can tighten.
            grace_seconds=GRACE_SECONDS,
        )


def _as_float(raw: Any, fallback: float, name: str) -> float:
    """Coerce a configured budget, falling back loudly-but-gracefully."""
    if raw in (None, ""):
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number; using %ss instead.", name, raw, fallback
        )
        return fallback


def _as_int(raw: Any, fallback: int, name: str) -> int:
    """Coerce a configured retry count and clamp it to the ceiling."""
    if raw in (None, ""):
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a whole number; using %s instead.", name, raw, fallback
        )
        return fallback
    if value < 0:
        logger.warning("%s=%r is negative; treating it as no retries.", name, raw)
        return 0
    if value > MAX_RETRIES_CEILING:
        logger.warning(
            "%s=%r is above the ceiling of %d; clamping. Retries repeat side "
            "effects, so an accidental extra digit is expensive.",
            name,
            value,
            MAX_RETRIES_CEILING,
        )
        return MAX_RETRIES_CEILING
    return value


#: ``await on_retry(attempt_number, exception)`` — called before each retry.
RetryHook = Callable[[int, BaseException], Awaitable[None]]


async def run_with_policy(
    call: Callable[[], Awaitable[Any]],
    *,
    policy: AttemptPolicy,
    label: str = "",
    never_retry: tuple[type[BaseException], ...] = (),
    on_retry: RetryHook | None = None,
) -> Any:
    """Run ``call`` under ``policy``, retrying attempts that fail.

    ``never_retry`` names exception types that must propagate on their first
    failure whatever ``max_retries`` says. The compiler passes
    :class:`~langgraph.errors.GraphBubbleUp`: ``interrupt()`` raises one to pause
    a run, and retrying a pause asks the same reviewer twice.

    :class:`asyncio.CancelledError` needs no entry on that list. It derives from
    ``BaseException`` rather than ``Exception``, so the retry branch below cannot
    see it and cancelling a run stops it at once. That is relied upon, not
    incidental: widening the ``except`` to ``BaseException`` would turn an
    operator's cancel into "retry the thing they just cancelled".

    Each attempt gets the full budget. A timed-out attempt *is* retryable — the
    usual reason a node times out is a provider or endpoint that hung, which is
    exactly what a second attempt fixes.
    """
    attempt = 0
    while True:
        try:
            return await _run_once(call, policy, label)
        except never_retry:
            raise
        except Exception as exc:  # noqa: BLE001 - the policy decides, not the type
            if attempt >= policy.max_retries:
                raise
            attempt += 1
            if on_retry is not None:
                await on_retry(attempt, exc)


async def _run_once(
    call: Callable[[], Awaitable[Any]], policy: AttemptPolicy, label: str
) -> Any:
    """One attempt, bounded by the policy's deadline."""
    timeout = asyncio.timeout(policy.deadline_seconds)
    try:
        async with timeout:
            return await call()
    except TimeoutError:
        if not timeout.expired():
            # The node raised this itself — a socket read running out of time,
            # say. Its message names the real cause; reattributing it to the
            # budget here would hide that behind a generic one.
            raise
        who = f"'{label}' " if label else "the node "
        raise NodeTimeoutError(
            f"{who}exceeded its {policy.timeout_seconds:g}s time budget"
        ) from None


__all__ = [
    "AttemptPolicy",
    "ERROR_KEY",
    "GRACE_SECONDS",
    "KEY_MAX_RETRIES",
    "KEY_ON_ERROR",
    "KEY_TIMEOUT",
    "MAX_RETRIES_CEILING",
    "ON_ERROR_CONTINUE",
    "ON_ERROR_DEFAULT",
    "ON_ERROR_MODES",
    "ON_ERROR_STOP",
    "NodeTimeoutError",
    "RetryHook",
    "run_with_policy",
]
