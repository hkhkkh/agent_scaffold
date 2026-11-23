"""Alembic environment.

Where the database URL comes from
--------------------------------

Not from ``alembic.ini``. The location of the database is a runtime decision —
``DATABASE_URL`` if set, otherwise ``DATA_DIR/scaffold.db`` — and it differs
between the app, the test suite, and a deployment. Reading it from
:func:`app.settings.get_settings` means ``alembic upgrade head`` does what you
mean whichever directory you are pointed at, and there is exactly one place that
answers "which database is this".

The alternative (a hard-coded URL in the ini) creates a second answer that wins
silently, which is how someone applies a migration to the wrong file.

Why the models must be imported
-------------------------------

``target_metadata`` has to see every table. Importing :mod:`app.db.models`
registers them on ``SQLModel.metadata`` as a side effect, so a model that is
never imported is a model alembic will happily *drop* on the next autogenerate.
The ``# noqa: F401`` below is load-bearing, not tidiness.

This is why every table is declared in :mod:`app.db.models`, including the ones
whose behaviour lives elsewhere (``Schedule`` is the current example). A model in
a second module needs a second import here, and an import that is forgotten is
not a missing table -- it is autogenerate proposing to **drop** one that exists.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Registers every table on SQLModel.metadata. Without this import, autogenerate
# sees an empty metadata and proposes dropping the entire schema.
from app.db import models  # noqa: F401
from app.settings import get_settings

config = context.config

# Only configure logging if the ini actually declares it.
#
# `alembic.ini` deliberately has no `[loggers]`/`[formatters]` section, because
# the application configures logging and a second block would make the same code
# log differently under alembic. But `fileConfig` assumes those sections exist
# and dies with `KeyError: 'formatters'` otherwise -- so the call is guarded
# rather than removed, which keeps this working for anyone who adds them back.
if config.config_file_name is not None and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    """The URL the application would use right now."""
    return get_settings().resolved_database_url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _target_metadata():
    from sqlmodel import SQLModel

    return SQLModel.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of touching a database.

    ``--sql`` mode is how a production change gets reviewed before it runs, and
    it is also the only way to inspect a migration on a machine with no database.
    Batch mode is required for SQLite here for the same reason as online: without
    it, any ALTER-based migration renders as an error rather than as SQL.
    """
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_is_sqlite(url),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations to the configured database.

    ``render_as_batch`` on SQLite: SQLite cannot ``ALTER COLUMN``, so alembic's
    batch mode rewrites the table (create new, copy, drop, rename) instead. Left
    off, any future column change fails at apply time with an unhelpful
    "near ALTER: syntax error" — which is exactly the class of failure migrations
    exist to prevent.
    """
    url = _database_url()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=_target_metadata(),
            compare_type=True,
            render_as_batch=_is_sqlite(url),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
