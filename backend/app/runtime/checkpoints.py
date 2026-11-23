"""Where a run's resume state lives.

A run that pauses for a human is only resumable while LangGraph can still read
its checkpoints. Under an ``InMemorySaver`` created per run they die with the
process, so a backend restart silently turns every paused run into an
unresumable one — the run row still says ``interrupted``, and pressing Resume
answers "unknown run".

**Durable state is half of the fix.** The other half is rebuilding the run's
*context* in the new process, which is ``RunManager.hydrate`` in
``runtime/manager.py``. Neither half is useful alone, and they were built one
task apart: for a while the checkpoints were on disk and Resume still answered
"unknown run", because ``resume`` only looked in its in-memory map. Worth knowing
before reading this module as "resume works" — this module is "the state is
readable", and that is a different claim.

This module is the seam for the state half, and it deliberately does **not**
define a new interface. Upstream already has one — ``BaseCheckpointSaver`` — and
the task that specified this module says so:

    Use ``langgraph-checkpoint-sqlite``'s ``AsyncSqliteSaver`` (the official
    package), and do not implement ``BaseCheckpointSaver`` yourself — that is
    LangGraph's internal contract and version upgrades break it.

So what is here is only the two things upstream cannot know:

1. **Which saver a deployment gets**, from configuration, failing closed on an
   unrecognised name — the same shape as ``SANDBOX_RUNNER`` and ``RUN_STORE``.
2. **Its lifetime.** ``AsyncSqliteSaver`` owns an ``aiosqlite`` connection that
   has to be opened once for the process and closed on shutdown, which a
   per-run factory cannot express.

Hence a *provider* rather than a saver. The distinction is not ceremony: the two
modes have genuinely different lifetimes, and pretending otherwise is what would
make the rest of the system wrong.

Why the modes differ, and what that means for eviction
---------------------------------------------------
============================  ============================  ==================
                              memory                        sqlite (default)
============================  ============================  ==================
a run's checkpointer           its own ``InMemorySaver``     one shared saver
lifetime                       dies with the run             the process
survives a restart             no                            yes
after a run is released        nothing to clean up           its rows remain
============================  ============================  ==================

The last row is the one with teeth. ``RunContext.release`` drops the object
reference, which is sufficient for memory mode because the object *was* the
storage. With a shared saver it is not: the reference is one of many and the rows
stay. Left alone, a durable saver turns an in-memory leak into a disk leak —
which is why :meth:`CheckpointerProvider.forget` exists and why eviction calls it.

``adelete_thread`` is part of ``BaseCheckpointSaver``, so that cleanup is a
supported operation rather than a reach into private tables.

Why the durable database is its own file
----------------------------------------
``AsyncSqliteSaver.setup()`` creates the tables it needs. Pointing it at the
platform's ``scaffold.db`` would mean those tables appear in a database that
``tests/test_migrations.py::TestTheTwoPathsAgree`` compares against our own model
definitions — so we would have to mirror LangGraph's internal schema in
``app/db/models.py`` and regenerate a migration every time it changed. That is
exactly the coupling the task file warns about, one level down.

A second file avoids it: ``data/checkpoints.db`` is LangGraph's schema, owned and
migrated by LangGraph, and our migrations never describe it. The cost is one more
file to back up, which is a smaller price than describing someone else's tables.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Accepted values of ``CHECKPOINT_STORE``. Constants so a caller cannot spell
#: them two ways — a typo would silently select the default, which is the failure
#: :class:`CheckpointerConfigError` exists to prevent.
CHECKPOINT_MEMORY = "memory"
CHECKPOINT_SQLITE = "sqlite"

#: Used when ``CHECKPOINT_STORE`` is set but empty — an unset environment variable
#: arrives as `""`, not as absent.
#:
#: It has to mirror the `Settings.checkpoint_store` default, and it lives here as
#: well because this module must stay importable without the application's
#: configuration. The duplication is not left to trust:
#: ``test_the_fallback_matches_the_setting_default`` fails if the two drift, which
#: is the same "one fact, one place, plus a test that the copies agree" pattern the
#: migration guard uses.
DEFAULT_CHECKPOINT_STORE = CHECKPOINT_SQLITE


class CheckpointerConfigError(ValueError):
    """Raised when ``CHECKPOINT_STORE`` names something that does not exist.

    An error rather than a fallback, matching ``SANDBOX_RUNNER`` and
    ``RUN_STORE``. The dangerous direction here is a typo for ``sqlite``:
    falling back to memory would give an operator a system that looks like it
    survives restarts and quietly does not, which they would discover only by
    losing a paused run.
    """


class CheckpointerProvider(ABC):
    """Supplies the checkpointer a run uses, and owns its lifetime."""

    #: Value of ``CHECKPOINT_STORE`` that selects this provider.
    name: ClassVar[str] = ""

    @abstractmethod
    def make(self) -> BaseCheckpointSaver:
        """The saver a new run should compile against.

        Called once per run. An implementation backed by a shared saver returns
        the same object every time; one backed by per-run state returns a new
        object.
        """

    async def open(self) -> None:
        """Acquire process-wide resources. Called once at startup."""

    async def close(self) -> None:
        """Release them. Called once at shutdown."""

    async def forget(self, thread_id: str) -> None:
        """Drop the checkpoints a finished run no longer needs.

        Called when a run is evicted. Default is nothing to do, which is correct
        for a per-run saver: dropping the reference drops the storage.
        """


class MemoryCheckpointerProvider(CheckpointerProvider):
    """A fresh in-memory saver per run.

    The default, and byte-for-byte the behaviour before this module existed.
    Chosen as the default deliberately: making durability opt-in keeps this
    change's blast radius at zero until the durable path has proven itself
    against the whole suite, and ``RunContext.release``'s reasoning about
    reclaiming memory stays exactly true.
    """

    name = CHECKPOINT_MEMORY

    def make(self) -> BaseCheckpointSaver:
        return InMemorySaver()


class SqliteCheckpointerProvider(CheckpointerProvider):
    """One shared, on-disk saver. A paused run survives a restart.

    Not thread-safe to construct and open concurrently — :meth:`open` is called
    once from the application lifespan, before any run exists.
    """

    name = CHECKPOINT_SQLITE

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._saver: BaseCheckpointSaver | None = None
        self._connection: object | None = None

    def make(self) -> BaseCheckpointSaver:
        if self._saver is None:
            # Reachable if a run is created before the lifespan opened us, e.g.
            # from a test that forgot to. Failing loudly beats returning a saver
            # that writes nowhere.
            raise CheckpointerConfigError(
                "The sqlite checkpointer was used before it was opened. "
                "It is opened in the application lifespan; a caller outside that "
                "lifespan must `await get_checkpointer_provider().open()` first."
            )
        return self._saver

    async def open(self) -> None:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(self.path))
        self._saver = AsyncSqliteSaver(self._connection)  # type: ignore[arg-type]
        # Creates the tables on first use and is a no-op afterwards, so it is safe
        # to call on every startup.
        await self._saver.setup()  # type: ignore[attr-defined]
        logger.info("Checkpoints  : %s", self.path)

    async def close(self) -> None:
        connection = self._connection
        self._saver = None
        self._connection = None
        if connection is not None:
            await connection.close()  # type: ignore[attr-defined]

    async def forget(self, thread_id: str) -> None:
        if self._saver is None:
            return
        await self._saver.adelete_thread(thread_id)  # type: ignore[attr-defined]


def build_checkpointer_provider(
    settings: Settings | None = None,
) -> CheckpointerProvider:
    """Construct the configured provider.

    Raises :class:`CheckpointerConfigError` for an unrecognised name.
    """
    settings = settings or get_settings()
    kind = (settings.checkpoint_store or DEFAULT_CHECKPOINT_STORE).strip().lower()

    if kind == CHECKPOINT_MEMORY:
        return MemoryCheckpointerProvider()
    if kind == CHECKPOINT_SQLITE:
        return SqliteCheckpointerProvider(settings.data_dir / "checkpoints.db")

    raise CheckpointerConfigError(
        f"CHECKPOINT_STORE={kind!r} is not a known store. Use "
        f"'{CHECKPOINT_MEMORY}' (this process only) or '{CHECKPOINT_SQLITE}' "
        "(on disk, so a paused run survives a restart)."
    )


#: Process-wide provider. Swapped in tests, and the seam where a Postgres or
#: Redis saver plugs in without touching the run manager.
_provider: CheckpointerProvider | None = None


def get_checkpointer_provider() -> CheckpointerProvider:
    global _provider
    if _provider is None:
        _provider = build_checkpointer_provider()
    return _provider


def set_checkpointer_provider(provider: CheckpointerProvider | None) -> None:
    """Override the process provider. ``None`` restores the configured one."""
    global _provider
    _provider = provider


__all__ = [
    "CHECKPOINT_MEMORY",
    "CHECKPOINT_SQLITE",
    "DEFAULT_CHECKPOINT_STORE",
    "CheckpointerConfigError",
    "CheckpointerProvider",
    "MemoryCheckpointerProvider",
    "SqliteCheckpointerProvider",
    "build_checkpointer_provider",
    "get_checkpointer_provider",
    "set_checkpointer_provider",
]
