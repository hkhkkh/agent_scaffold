"""Database session management."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import Run, RunStep, Workflow  # noqa: F401 - registers tables
from app.settings import get_settings

logger = logging.getLogger(__name__)

#: Cached per database URL rather than a single global. Tests point the data
#: directory at a temp folder, and a single cached engine would keep writing to
#: whichever directory happened to be configured first.
_engines: dict[str, Engine] = {}


def get_engine() -> Engine:
    """Return the engine for the currently configured database."""
    settings = get_settings()
    settings.ensure_dirs()
    url = settings.resolved_database_url

    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(
            url,
            echo=False,
            # SQLite + FastAPI: connections are reused across threads, so the
            # default same-thread check has to be relaxed.
            connect_args={"check_same_thread": False},
        )

        # WAL lets a read (listing runs) proceed while a write (recording a step)
        # is in flight, which matters because runs stream while the UI polls.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        _engines[url] = engine

    return engine


def dispose_engines() -> None:
    """Drop cached engines. Used by tests and on application shutdown."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


def create_all() -> None:
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Session:
    return Session(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session. Commits on success, rolls back on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "create_all",
    "dispose_engines",
    "get_engine",
    "get_session",
    "session_scope",
]
