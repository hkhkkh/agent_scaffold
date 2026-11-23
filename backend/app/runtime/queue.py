"""How many runs may execute at once, and what happens to the rest.

The problem
-----------
Without a limit, every run the platform accepts starts immediately. A single
workflow that blocks on a slow tool then takes a slot for as long as it likes, and
past a certain number of them the whole process is slow — including the request
that would have told you so. The failure is not a crash; it is everything getting
worse at the same time, which is much harder to notice.

A limit alone is not enough either. If runs are simply rejected at the limit, a
user who starts a batch gets errors instead of work. If they are silently dropped,
the UI shows a run that never starts and never says why. So the platform needs a
**queue**: a bound on how many execute, plus a visible place for the rest to wait,
plus a way to leave it.

Why not an `asyncio.Semaphore`
-----------------------------
A semaphore enforces the bound and nothing else. It cannot answer "who is
waiting?", so the UI cannot show a queue, and it cannot remove one waiter, so a
user cannot cancel a run they queued by mistake. Both of those are the feature.
The bound is the easy part.

Why not a job library (arq / dramatiq / RQ)
-------------------------------------------
They all require a broker process (usually Redis). An exported agent has to run
with what a customer already has — a SQLite file and Python — so a broker would
make the queue a deployment dependency rather than part of the product. The
interface below is deliberately small so a future `SqliteTaskQueue` or a broker
backed one can replace the in-memory implementation without touching the manager.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.settings import Settings, get_settings

#: Accepted values of ``TASK_QUEUE``.
QUEUE_MEMORY = "memory"


class QueueConfigError(ValueError):
    """Raised when ``TASK_QUEUE`` names something that does not exist.

    An error rather than a fallback, matching ``RUN_STORE`` / ``SANDBOX_RUNNER``:
    a deployment that asked for a queue it cannot have should stop with a message,
    not quietly run unbounded.
    """


class RunCancelledWhileQueued(Exception):
    """Raised out of :meth:`TaskQueue.acquire` when the wait was cancelled.

    A dedicated exception rather than ``asyncio.CancelledError``: the caller is
    inside a task that should *finish* (recording the run as cancelled), not one
    that should unwind. Using the CancelledError would make the cancellation
    propagate into the task machinery, where it means "this task is being thrown
    away" — a different thing with different cleanup.
    """


class TaskQueue(ABC):
    """Decides which run executes now and which one waits."""

    #: Value of ``TASK_QUEUE`` that selects this implementation.
    name: ClassVar[str] = ""

    @property
    @abstractmethod
    def limit(self) -> int:
        """Maximum concurrent runs. ``0`` means unlimited."""

    @abstractmethod
    async def acquire(self, run_id: str) -> None:
        """Wait for a free slot belonging to ``run_id``.

        Raises :class:`RunCancelledWhileQueued` if the wait was abandoned.
        """

    @abstractmethod
    def try_acquire(self, run_id: str) -> bool:
        """Take a slot **only if one is free right now**.

        Exists so "this run had to wait" is a fact rather than a guess. Without
        it the manager would have to mark every run as queued and then un-mark the
        ones that started immediately, which emits a queue event for runs that
        never queued — noise in the timeline and in every event-ordering test.
        """

    @abstractmethod
    def release(self, run_id: str) -> None:
        """Give back the slot. Safe to call for a run that never had one."""

    @abstractmethod
    def drop(self, run_id: str) -> bool:
        """Remove a *waiting* run. Returns whether it was still waiting.

        Returns ``False`` for a run that is already executing, because stopping
        one of those is a different operation (:meth:`RunManager.cancel`).
        """

    @abstractmethod
    def position(self, run_id: str) -> int | None:
        """1-based place in line, or ``None`` if not waiting."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Counts and the waiting ids, for the UI and for tests."""


class InMemoryTaskQueue(TaskQueue):
    """A bound in this process, with a visible FIFO and removable waiters.

    In-process is the right default here for the same reason the run store's
    default is SQLite: this is one backend serving one operator. The interface
    exists so that stops being true without a rewrite.
    """

    name = QUEUE_MEMORY

    def __init__(self, limit: int = 0) -> None:
        self._limit = max(0, int(limit))
        self._active: set[str] = set()
        self._waiting: list[str] = []
        self._dropped: set[str] = set()
        #: One condition for both events that free a slot: a release, and a
        #: waiter leaving. Two conditions would need to be notified in lockstep
        #: and would drift the first time one path forgot.
        self._changed = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    def try_acquire(self, run_id: str) -> bool:
        """Synchronous by necessity: it must not yield, or two runs could both
        see the last slot as free before either takes it.

        ``not self._waiting`` keeps FIFO honest — a run arriving after others are
        already in line must join the line rather than take a slot that just
        came free.
        """
        if self._limit == 0:
            self._active.add(run_id)
            return True
        if len(self._active) < self._limit and not self._waiting:
            self._active.add(run_id)
            return True
        return False

    async def acquire(self, run_id: str) -> None:
        if self._limit == 0:
            self._active.add(run_id)
            return

        async with self._changed:
            if run_id not in self._waiting:
                self._waiting.append(run_id)
            while True:
                if run_id in self._dropped:
                    self._dropped.discard(run_id)
                    self._forget(run_id)
                    raise RunCancelledWhileQueued(run_id)
                # Strict FIFO: even a run whose slot is free waits for anyone
                # ahead of it. Allowing overtaking would let a burst of short
                # runs starve the one at the front.
                if (
                    self._waiting
                    and self._waiting[0] == run_id
                    and len(self._active) < self._limit
                ):
                    self._waiting.pop(0)
                    self._active.add(run_id)
                    self._changed.notify_all()
                    return
                await self._changed.wait()

    def release(self, run_id: str) -> None:
        """Not async, because the manager calls it from a ``finally`` that may be
        running while the event loop is shutting down.

        The notification is scheduled rather than awaited: a slot that frees
        microseconds later is still freed, and a queue drain during shutdown does
        not depend on this call completing.
        """
        self._active.discard(run_id)
        self._forget(run_id)
        self._notify()

    def drop(self, run_id: str) -> bool:
        if run_id not in self._waiting:
            return False
        self._dropped.add(run_id)
        self._waiting.remove(run_id)
        self._notify()
        return True

    def position(self, run_id: str) -> int | None:
        try:
            return self._waiting.index(run_id) + 1
        except ValueError:
            return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limit": self._limit,
            "active": sorted(self._active),
            "waiting": list(self._waiting),
        }

    # ------------------------------------------------------------- internals
    def _forget(self, run_id: str) -> None:
        self._dropped.discard(run_id)
        if run_id in self._waiting:
            self._waiting.remove(run_id)

    def _notify(self) -> None:
        """Wake every waiter, from sync code.

        The lock is taken with ``try``/``except`` rather than awaited because the
        caller may be outside a running loop (a `finally` during shutdown). A
        missing notification is not worth an exception there — the next
        ``acquire`` re-checks the condition anyway, so the cost is a wake-up that
        arrives on the next event rather than immediately.
        """
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_closed():
            return
        try:
            task = loop.create_task(self._wake())
        except RuntimeError:
            return
        task.add_done_callback(lambda _: None)

    async def _wake(self) -> None:
        async with self._changed:
            self._changed.notify_all()


# ---------------------------------------------------------------------------
# Injection point
# ---------------------------------------------------------------------------

_queue: TaskQueue | None = None


def build_task_queue(settings: Settings) -> TaskQueue:
    """Construct the configured queue. Fails loudly on an unknown name."""
    kind = (settings.task_queue or QUEUE_MEMORY).strip().lower()
    if kind == QUEUE_MEMORY:
        return InMemoryTaskQueue(settings.max_concurrent_runs)
    raise QueueConfigError(
        f"TASK_QUEUE='{kind}' is not a known queue. Available: '{QUEUE_MEMORY}'."
    )


def get_task_queue() -> TaskQueue:
    """The process-wide queue, built from settings on first use."""
    global _queue
    if _queue is None:
        _queue = build_task_queue(get_settings())
    return _queue


def set_task_queue(queue: TaskQueue | None) -> None:
    """Install a queue. Tests use this to pin a known limit."""
    global _queue
    _queue = queue


__all__ = [
    "QUEUE_MEMORY",
    "InMemoryTaskQueue",
    "QueueConfigError",
    "RunCancelledWhileQueued",
    "TaskQueue",
    "build_task_queue",
    "get_task_queue",
    "set_task_queue",
]
