"""Per-run event bus.

A run produces events faster than a browser can reliably consume them, and a
client may connect *after* the run has started (or reconnect mid-run). So the bus
is a replay buffer plus live subscribers rather than a plain queue:

* every event is appended to a bounded history,
* a new subscriber replays the history first, then receives live events,
* a slow subscriber is dropped rather than allowed to stall the run.

Sequence numbers are assigned here, so the client can order and de-duplicate even
if it reconnects.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.graph.schema import RunEvent, RunEventType
from app.runtime.serialize import jsonable

#: Cap on replayed history. A long run with token streaming produces thousands of
#: chunk events; keeping them all would grow memory without bound. The tail is
#: what a reconnecting client needs.
MAX_HISTORY = 2_000

#: Chunk events are the bulk of the traffic and are worthless once superseded, so
#: they are kept only in a small tail. Everything else is kept in full.
MAX_CHUNK_HISTORY = 200

#: Subscriber queue depth. Beyond this the client is considered stalled.
SUBSCRIBER_QUEUE_SIZE = 512

#: Terminal event types — seeing one ends a subscription.
_TERMINAL = frozenset({RunEventType.RUN_FINISHED, RunEventType.RUN_FAILED})


class EventBus:
    """Fan-out of :class:`RunEvent` to any number of subscribers."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._seq = 0
        self._history: list[RunEvent] = []
        self._chunk_history: list[RunEvent] = []
        self._subscribers: set[asyncio.Queue[RunEvent | None]] = set()
        self._closed = False
        #: Sequence number of the oldest event still retained. Non-zero only
        #: after :meth:`trim` has discarded a run's early history.
        self._earliest_seq = 0

    # ------------------------------------------------------------------ emit
    async def emit(
        self,
        event_type: RunEventType,
        *,
        node_id: str | None = None,
        node_label: str | None = None,
        step: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Record an event and push it to every live subscriber."""
        self._seq += 1
        event = RunEvent(
            type=event_type,
            run_id=self.run_id,
            seq=self._seq,
            node_id=node_id,
            node_label=node_label,
            step=step,
            timestamp=datetime.now(timezone.utc),
            # `jsonable` here means a node returning a LangChain object can never
            # crash the event bus.
            data=jsonable(data or {}),
        )

        if event_type is RunEventType.NODE_OUTPUT_CHUNK:
            self._chunk_history.append(event)
            if len(self._chunk_history) > MAX_CHUNK_HISTORY:
                del self._chunk_history[:-MAX_CHUNK_HISTORY]
        else:
            self._history.append(event)
            if len(self._history) > MAX_HISTORY:
                del self._history[:-MAX_HISTORY]

        self._publish(event)
        return event

    def _publish(self, event: RunEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client must not slow the run down. Drop it; the SSE
                # endpoint will notice the queue is gone and can re-subscribe
                # with `after` to replay what it missed.
                self._subscribers.discard(queue)

    # ------------------------------------------------------------- subscribe
    async def subscribe(self, after: int = 0) -> AsyncIterator[RunEvent]:
        """Yield events, replaying history first.

        ``after`` is the last sequence number the client has already seen, which
        makes reconnection cheap and lossless.
        """
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_SIZE
        )

        replay = [
            event for event in (*self._history, *self._chunk_history) if event.seq > after
        ]
        replay.sort(key=lambda event: event.seq)

        self._subscribers.add(queue)
        try:
            for event in replay:
                yield event

            if self._closed:
                return

            while True:
                event = await queue.get()
                if event is None:
                    # Bus closed while we were waiting.
                    return
                yield event
                if event.type in _TERMINAL:
                    return
        finally:
            self._subscribers.discard(queue)

    # ----------------------------------------------------------------- state
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def earliest_seq(self) -> int:
        """Oldest sequence number still available for replay (1 once emitting).

        A subscriber asking for ``after`` older than ``earliest_seq - 1`` will
        receive a gap: it is told the replay starts here, not that it is complete.
        """
        return self._earliest_seq or 1

    def history(self) -> list[RunEvent]:
        """All retained events, ordered. Used to write the run transcript."""
        combined = sorted([*self._history, *self._chunk_history], key=lambda e: e.seq)
        return combined

    def trim(self, keep: int) -> int:
        """Discard all but the newest ``keep`` events. Returns how many went.

        Called once a run has finished, when the reason for holding a deep replay
        buffer (catching up a subscriber that connected mid-run) no longer exists.
        The transcript must already be written — this is destructive.

        Note this can create a **gap** for a subscriber that reconnects with an
        ``after`` older than what is retained. That is a deliberate trade: the
        events are gone from memory, but they are fully intact in the transcript
        file and in the ``RunStep`` table, which is where a client should rebuild
        from. :attr:`earliest_seq` is what lets a caller detect the gap instead of
        silently presenting a truncated replay as a complete one.
        """
        if keep < 0:
            raise ValueError("keep must be >= 0")
        combined = self.history()
        if len(combined) <= keep:
            return 0

        dropped = combined[: len(combined) - keep]
        retained = combined[len(combined) - keep :]
        self._history = [e for e in retained if e.type is not RunEventType.NODE_OUTPUT_CHUNK]
        self._chunk_history = [
            e for e in retained if e.type is RunEventType.NODE_OUTPUT_CHUNK
        ]
        # `_earliest_seq` is 1-based, matching `RunEvent.seq` which starts at 1.
        self._earliest_seq = retained[0].seq
        return len(dropped)

    def close(self) -> None:
        """Mark the run finished so waiting subscribers stop blocking.

        Pushes a ``None`` sentinel so each subscriber wakes up and returns, even
        if the run ended without emitting a terminal event (e.g. cancelled).
        """
        if self._closed:
            return
        self._closed = True
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


__all__ = ["EventBus", "MAX_CHUNK_HISTORY", "MAX_HISTORY"]
