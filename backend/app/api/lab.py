"""Read-only view of the results database.

One endpoint, and the reason it is an endpoint at all rather than something the
frontend derives from the run's event stream: an agent writes rows *while it is
thinking*, and the thing worth watching is not any single node's output but the
**table growing**. The event stream carries per-node payloads, so a panel built
on it would show the last write and not the accumulation.

So the panel polls a snapshot. Polling rather than SSE, deliberately: the writer
is a run that may not even be the one in the foreground, the panel must also be
correct *after* a run finishes and after a page reload, and a snapshot is
idempotent in a way a stream is not. One query a second against a local SQLite
file is not a cost worth engineering around.

The route is namespaced under ``/lab`` rather than ``/db`` so it reads as what it
is — a window onto the *workflow-facing* results store, distinct from the
platform's own tables in ``scaffold.db``, which no endpoint exposes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from app.tools import results_db

router = APIRouter(prefix="/lab", tags=["lab"])

PreviewRows = Query(
    default=8,
    ge=0,
    le=100,
    description="How many most-recent rows to include per table.",
)


@router.get("/db", summary="Snapshot of the results database")
async def read_db(preview: int = PreviewRows) -> dict[str, Any]:
    """Everything the live panel needs, in one request.

    ``asyncio.to_thread`` because ``snapshot`` is synchronous sqlite3 work and
    would otherwise block the event loop — which during a run means blocking the
    very events the rest of the UI is waiting on. Same reasoning as
    ``app.tools.results_db``'s own tool wrappers; the call is routed through
    them rather than duplicating it.
    """
    return await asyncio.to_thread(results_db.snapshot, preview)
