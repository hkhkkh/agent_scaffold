"""Where a run's history is recorded.

The run framework has to remember what happened, and the platform wants to show
that history in a UI. Those are not the same concern, and until now the framework
reached directly for the platform's database — ``runtime/manager.py`` imported
``app.db``, which pulled SQLModel, then the session stack, then FastAPI, into
anything that wanted to execute a graph.

That arrow pointed the wrong way. This module is the seam that removes it: the
framework depends on the interface below, and whichever deployment is running
decides what sits behind it.

Why this matters more than it looks
-----------------------------------
An exported agent is supposed to run without the canvas platform. Today it could
not, because importing it imported the platform's database stack. That is not a
performance question — it is the difference between "the agent is a standalone
program" and "the agent is a piece of the platform that was copied out".

Three of the four implementations exist to make that distinction concrete:

* ``SqliteRunStore`` (in ``app/db/run_store.py``) — the platform's choice and the
  default. Keeps behaviour byte-for-byte identical to before this module existed.
* :class:`MemoryRunStore` — tests, and any caller that wants history without a
  database.
* :class:`NullRunStore` — **an exported agent**, which has no reason to keep a
  local run history at all. Its existence is the point: "don't record history" is
  a supported choice rather than an error.

The SQLite implementation lives on the platform side, not in this file, and that
placement is deliberate. If it sat here, the runtime package would contain a
module importing ``app.db`` — a package that does not exist inside an exported
agent.

The first attempt at that seam was a lazy import inside
:func:`build_run_store` — ``from app.db.run_store import SqliteRunStore`` in the
``sqlite`` branch. It kept the reference out of *this process's* import graph,
which is not the same as keeping it out of the *file*. Task 27a builds an agent
package by copying sources, and a copied file carries every line it contains,
including the one behind an ``if``. So the branch now calls the class the
platform injects (:func:`set_database_run_store`): the name ``sqlite`` stays
here, and the module path that implements it never does.

Why the methods take plain dicts, not a ``RunContext``
-----------------------------------------------------
The obvious interface is ``start(context)`` / ``finish(context)``: the manager has
a context, the store wants the data, hand it over. That version has a trap worth
naming, because taking it would have been a **security regression**.

The rows that get written must pass through ``redact`` — a node's config can hold
an API key, and step snapshots are raw node inputs and outputs. Redaction belongs
to the manager, which is the boundary that knows a credential when it sees one. If
the store received a live context it would have to do the redacting itself, and
then every implementation would have to get it right; the day one of them added a
field the other did not, the safer of the two would be the only one redacting.
The alternative — the manager redacts before calling — is not expressible,
because the context is passed by reference and mutating it would rewrite a live
run's own inputs.

So the manager builds the payloads, redacting as it does, and the store persists
what it is given. One redaction point, testable without a store, and nothing left
for the store to decide. The cost is one small layer, and it buys a contract a new
backend can be written against without knowing what a ``RunContext`` is.

What an implementation owes
---------------------------
1. **Never raise on a happy path.** Callers wrap these in ``except Exception``
   because failing to record history must not abort a run that is otherwise fine.
   That net stays, but a store that throws routinely turns it into a permanent lie.
2. **Store what you are handed.** Payloads arrive already redacted. Redacting
   again would not add safety — it would hide a real value from a deployment that
   legitimately stores it, and let the write paths drift apart.
3. **Return wire-shaped dicts.** :meth:`RunStore.summary`, :meth:`RunStore.steps`,
   and :meth:`RunStore.history` must use the same key names as the live-run
   snapshots in ``api/runs.py``, so a client cannot tell which one answered.
4. **Be safe to call for an unknown id.** Return ``None`` or an empty list; the
   decision to answer 404 belongs to the HTTP layer, not here.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Accepted values of ``RUN_STORE``. Kept as constants so a caller cannot spell
#: them differently in two places — a typo would silently select the default,
#: which is exactly the failure :class:`RunStoreConfigError` exists to prevent.
RUN_STORE_SQLITE = "sqlite"
RUN_STORE_MEMORY = "memory"
RUN_STORE_NULL = "null"

#: Keys a ``start`` payload carries. Published because the manager builds it and
#: a new backend has to read it; a silent rename would surface as a KeyError
#: inside the ``except Exception`` that guards persistence, i.e. as silence.
RUN_START_FIELDS = (
    "id",
    "workflow_id",
    "status",
    "inputs",
    "thread_id",
    "tweaks",
    "recursion_limit",
    "created_at",
)

#: Keys a ``finish`` payload carries — the start set plus the terminal ones.
#: Two tuples rather than one so a payload never contains a key its reader
#: ignores: an unread key is where the two sides start drifting apart.
RUN_FINISH_FIELDS = (*RUN_START_FIELDS, "outputs", "error", "finished_at")

#: Keys ``load`` returns for rebuilding a run in a new process.
#:
#: Deliberately **not** the same set as :func:`RunStore.summary`. ``summary`` is a
#: wire shape a client sees, and it is compared key-for-key against the live-run
#: summary; this one carries the internals rehydration needs. Merging them would
#: put ``tweaks`` into an API response and break that comparison.
RUN_LOAD_FIELDS = (
    "id",
    "workflow_id",
    "status",
    "inputs",
    "thread_id",
    "tweaks",
    "recursion_limit",
    "created_at",
)

#: Keys a ``step`` payload carries, mirroring one ``RunStep`` row.
STEP_FIELDS = (
    "run_id",
    "node_id",
    "node_label",
    "index",
    "status",
    "inputs",
    "outputs",
    "error",
    "started_at",
    "finished_at",
    "duration_ms",
    # The lines the node emitted. Part of the step rather than a side table
    # because a reader always wants them together with the step that produced
    # them, and they were being dropped entirely — see
    # `manager.StepRecord.logs`.
    "logs",
)


class RunStoreConfigError(ValueError):
    """Raised when ``RUN_STORE`` names something that does not exist.

    An error rather than a fallback, matching ``SANDBOX_RUNNER``. Falling back to
    SQLite would mean an operator who asked for ``null`` because their environment
    has no database gets a startup that looks fine, followed by runs that die
    writing history they were told not to keep. Refusing to start says the same
    thing loudly, and immediately.
    """


class RunStore(ABC):
    """Records a run's progress, and answers questions about past runs."""

    #: Value of ``RUN_STORE`` that selects this implementation.
    name: ClassVar[str] = ""

    # ------------------------------------------------------------------ write
    def start(self, run: dict[str, Any]) -> None:
        """Record that a run has begun. Called once, before the graph executes."""

    def finish(self, run: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        """Record a run's terminal state and its per-node steps.

        Called from the run's ``finally`` block, so it sees every outcome,
        including a cancellation.
        """

    # ------------------------------------------------------------------- read
    @abstractmethod
    def exists(self, run_id: str) -> bool:
        """Has this run id ever been recorded?

        Used to tell "this run finished and was evicted from memory" apart from
        "this run id was never real" — 410 versus 404.
        """

    @abstractmethod
    def summary(self, run_id: str) -> dict[str, Any] | None:
        """One run, shaped like a live-run summary. ``None`` if never recorded."""

    def load(self, run_id: str) -> dict[str, Any] | None:
        """One run with everything needed to **rebuild** it in a new process.

        Distinct from :meth:`summary`: that one is a wire shape, this one carries
        the internals rehydration needs (``tweaks``, ``recursion_limit``).

        Defaults to ``None``, which is correct for a store that keeps nothing —
        and is why an exported agent answers "cannot be rebuilt" rather than
        rebuilding a run with the wrong configuration.
        """
        return None

    @abstractmethod
    def steps(self, run_id: str) -> list[dict[str, Any]]:
        """A run's per-node steps in execution order. Empty if never recorded."""

    @abstractmethod
    def history(
        self, workflow_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recorded runs, newest first, optionally filtered by workflow."""


class NullRunStore(RunStore):
    """Records nothing, and says so consistently.

    What an exported agent gets. A standalone program that runs one workflow on a
    schedule has no reader for a run history, so writing one is storage and I/O
    for nobody — and it would mean shipping a database to a place that does not
    want one.

    ``exists`` answers ``False`` rather than raising, which has one visible
    consequence worth stating: an evicted run reads as an unknown run (404) rather
    than as a gone one (410). That is correct here — with no history there is
    genuinely nothing to go back to — and it only affects the HTTP surface, which
    an exported agent does not serve.
    """

    name = RUN_STORE_NULL

    def exists(self, run_id: str) -> bool:
        return False

    def summary(self, run_id: str) -> dict[str, Any] | None:
        return None

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        return []

    def history(
        self, workflow_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return []


class MemoryRunStore(RunStore):
    """Keeps history in dictionaries. For tests, and for embedded use.

    Mirrors the SQLite store's output shapes exactly, including that
    :meth:`finish` appends the steps it is given rather than replacing earlier
    ones. That is a deliberate copy of the existing behaviour rather than an
    improvement: if the two stores disagreed about being called twice, a test
    could pass against one and production would fail against the other.
    """

    name = RUN_STORE_MEMORY

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._steps: dict[str, list[dict[str, Any]]] = {}

    def start(self, run: dict[str, Any]) -> None:
        self._runs[run["id"]] = {
            "id": run["id"],
            "workflow_id": run.get("workflow_id"),
            "status": run.get("status"),
            "inputs": run.get("inputs"),
            "outputs": {},
            "error": None,
            "thread_id": run.get("thread_id"),
            "tweaks": run.get("tweaks") or {},
            "recursion_limit": run.get("recursion_limit"),
            "created_at": run.get("created_at"),
            "finished_at": None,
        }

    def finish(self, run: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        row = self._runs.get(run["id"])
        if row is not None:
            row["status"] = run.get("status")
            row["outputs"] = run.get("outputs")
            row["error"] = run.get("error")
            row["finished_at"] = run.get("finished_at")

        bucket = self._steps.setdefault(run["id"], [])
        for step in steps:
            bucket.append(
                {
                    "node_id": step.get("node_id"),
                    "node_label": step.get("node_label"),
                    "index": step.get("index"),
                    "status": step.get("status"),
                    "inputs": step.get("inputs"),
                    "outputs": step.get("outputs"),
                    "error": step.get("error"),
                    "duration_ms": step.get("duration_ms"),
                    "logs": step.get("logs") or [],
                }
            )

    def exists(self, run_id: str) -> bool:
        return run_id in self._runs

    def summary(self, run_id: str) -> dict[str, Any] | None:
        row = self._runs.get(run_id)
        if row is None:
            return None
        return {**row, "duration_ms": elapsed_ms(row)}

    def load(self, run_id: str) -> dict[str, Any] | None:
        row = self._runs.get(run_id)
        if row is None:
            return None
        return {name: row.get(name) for name in RUN_LOAD_FIELDS}

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(step) for step in self._steps.get(run_id, [])]

    def history(
        self, workflow_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self._runs.values()
            if workflow_id is None or row["workflow_id"] == workflow_id
        ]
        # Newest first, matching `ORDER BY created_at DESC`. Timestamps can tie
        # inside one test, so the id breaks the tie rather than dict insertion
        # order, which would look stable and not be.
        rows.sort(key=lambda row: (str(row.get("created_at")), row["id"]), reverse=True)
        return [dict(row) for row in rows[:limit]]


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def build_run_store(settings: Settings | None = None) -> RunStore:
    """Construct the configured store.

    Raises :class:`RunStoreConfigError` for an unrecognised ``RUN_STORE``.

    The database-free branches live here, in layer 2. The one that needs a
    database does **not**: it is supplied by the platform through
    :func:`set_database_run_store`. This module used to hold a lazy
    ``from app.db.run_store import SqliteRunStore`` instead, on the theory that a
    lazy import keeps the platform out of the import graph — which it does, and
    which turned out not to be the question. A copy of the module carries the
    line regardless, so a wheel built for task 27a would name a package that does
    not exist in an exported agent. The line had to go, not move.

    So the platform injects the class, exactly as it does for the checkpointer
    (``set_checkpointer_provider``) and the subgraph loader
    (``set_subgraph_provider``): the ``sqlite`` name stays here, the module path
    that implements it never does.
    """
    settings = settings or get_settings()
    kind = (settings.run_store or RUN_STORE_SQLITE).strip().lower()

    if kind == RUN_STORE_NULL:
        return NullRunStore()
    if kind == RUN_STORE_MEMORY:
        return MemoryRunStore()
    if kind == RUN_STORE_SQLITE:
        if _database_store is None:
            raise RunStoreConfigError(
                "RUN_STORE='sqlite' needs a database-backed store, and none is "
                "registered in this deployment. The platform registers one at "
                "startup; a standalone agent is expected to run without history "
                "— use RUN_STORE=null, or install one via "
                "`set_database_run_store`."
            )
        return _database_store()

    raise RunStoreConfigError(
        f"RUN_STORE={kind!r} is not a known store. Use "
        f"'{RUN_STORE_SQLITE}' (the database), '{RUN_STORE_MEMORY}' (this process "
        f"only), or '{RUN_STORE_NULL}' (record nothing)."
    )


#: The store class this module cannot import: ``app.db.run_store.SqliteRunStore``.
#: ``None`` means no database backend is available, which is the normal state of
#: an exported agent rather than a fault — see the module docstring.
_database_store: Callable[[], RunStore] | None = None


def set_database_run_store(store: Callable[[], RunStore] | None) -> None:
    """Install (or clear) the database-backed store. ``None`` restores "absent".

    Called from ``app/main.py``, which is the only layer allowed to know that
    ``sqlite`` means ``app.db.run_store``. A callable rather than a class so a
    backend that needs ``Settings`` can close over it.
    """
    global _database_store
    _database_store = store


#: Process-wide store. Swapped in tests, and the seam where a Postgres-backed or
#: remote store plugs in without touching the run manager.
_store: RunStore | None = None


def get_run_store() -> RunStore:
    global _store
    if _store is None:
        _store = build_run_store()
    return _store


def set_run_store(store: RunStore | None) -> None:
    """Override the process store. Passing ``None`` restores the configured one."""
    global _store
    _store = store


# ---------------------------------------------------------------------------
# Shaping helpers
# ---------------------------------------------------------------------------


def elapsed_ms(row: dict[str, Any]) -> float | None:
    """Duration from a stored row, or ``None`` when it never finished.

    Public because both the memory store and ``api/runs.py`` need it, and two
    copies of a rounding rule is how one duration ends up reported two ways.
    """
    started = row.get("created_at")
    finished = row.get("finished_at")
    if started is None or finished is None:
        return None
    try:
        return round((finished - started).total_seconds() * 1000, 1)
    except (AttributeError, TypeError):
        return None


__all__ = [
    "MemoryRunStore",
    "NullRunStore",
    "RUN_FINISH_FIELDS",
    "RUN_LOAD_FIELDS",
    "RUN_START_FIELDS",
    "RUN_STORE_MEMORY",
    "RUN_STORE_NULL",
    "RUN_STORE_SQLITE",
    "STEP_FIELDS",
    "RunStore",
    "RunStoreConfigError",
    "build_run_store",
    "elapsed_ms",
    "get_run_store",
    "set_run_store",
    "set_database_run_store",
]
