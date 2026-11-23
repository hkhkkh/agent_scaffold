"""Schema management: one call the application makes, migrations underneath.

Why this module exists
----------------------

Before it, the application created its schema with ``SQLModel.metadata.create_all()``
and nothing else. That works exactly once — on a database that does not exist yet.
It cannot add a column, cannot rename one, and cannot express a data migration, so
the first schema change (the memory tables in Phase 3) would have meant telling
every existing deployment to delete their database.

Alembic is the answer to that, but alembic alone leaves two problems:

1. **Existing databases have no ``alembic_version`` row.** They were created by
   ``create_all`` and know nothing about revisions. Running ``alembic upgrade``
   against one would try to create tables that already exist.
2. **Tests** build a throwaway database per test. Running migrations for each
   would make the suite measurably slower for no added coverage — the schema they
   want is the current one, and they do not care how it was reached.

So this module owns the decision, and it is a decision rather than a rule:

* a database alembic already manages -> ``upgrade head``
* anything else -> ``create_all()`` for the current schema, then *stamp* it as
  being at head, so the next start takes the first branch

That last step is the adoption pattern, and it is why the two paths can coexist
safely. What makes it *correct* rather than merely convenient is
``tests/test_migrations.py``: it builds the schema both ways and asserts the
results are identical, so a model change that is missing from a migration, or a
migration that no longer matches the models, fails the suite instead of failing
in production.

The honest limitation
---------------------

``create_all()`` builds the *current* schema, not the initial one. A database
created today by the second branch is at head only because the schema it just got
is the head schema. That is exactly right for a fresh database and exactly wrong
for one that has been running for a while — which is why the branch is chosen by
"does alembic already manage this?" rather than by "are there tables?".
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db.session import create_all, get_engine
from app.settings import get_settings

logger = logging.getLogger(__name__)

#: `backend/`, i.e. the directory `alembic.ini` lives in. Derived from this file
#: rather than from the working directory: uvicorn is started from several places
#: in this project (`--app-dir`, a task, a script), and a relative path would
#: resolve differently in each.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

VERSION_TABLE = "alembic_version"


def alembic_config() -> Config:
    """The alembic configuration, with the URL taken from the app settings.

    ``env.py`` also reads the URL from settings; setting it here as well means
    the value is available to any alembic API that inspects the config before
    ``env.py`` runs (``ScriptDirectory``, for instance) without duplicating the
    resolution logic.
    """
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", get_settings().resolved_database_url)
    return config


def head_revision() -> str | None:
    """The revision the migration scripts consider current.

    Read from the scripts, not from the database — this is what "upgrade to head"
    means before anything has been upgraded.
    """
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def head_revisions() -> tuple[str, ...]:
    """Every head. More than one means a divergence that needs a merge revision.

    Exposed separately because "there are two heads" is a distinct failure with a
    distinct fix, and collapsing it into :func:`head_revision` would report the
    wrong thing (`get_current_head` raises, or silently picks one).
    """
    return tuple(ScriptDirectory.from_config(alembic_config()).get_heads())


def current_revision() -> str | None:
    """The revision this database is at, or ``None`` if alembic does not manage it."""
    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()


def is_managed() -> bool:
    """Whether this database is under alembic's control.

    Checked by looking for the version table rather than by asking for the current
    revision: ``get_current_revision()`` returns ``None`` both for "no version
    table" and for "version table present but empty", and those need different
    handling. An empty version table is a half-finished ``stamp`` and should not
    be treated as an unmanaged database.
    """
    return VERSION_TABLE in inspect(get_engine()).get_table_names()


def upgrade_to_head() -> str | None:
    """Apply every pending migration. Returns the revision now in effect."""
    command.upgrade(alembic_config(), "head")
    return current_revision()


def stamp_head() -> None:
    """Record this database as being at head without running anything.

    Used only for a database whose schema was just built by ``create_all()``.
    Stamping a database that is *not* actually at head is the one way to make
    alembic lie, so nothing calls this except :func:`ensure_schema`'s fresh-schema
    branch.
    """
    command.stamp(alembic_config(), "head")


def ensure_schema() -> str:
    """Bring the database to head, whatever state it is in. Returns how.

    Called from the application lifespan. Returns a short label describing which
    branch was taken, so startup can log it — "which path did my deployment take"
    is the first question when a schema issue appears, and it is unanswerable
    after the fact if nothing records it.
    """
    if is_managed():
        revision = upgrade_to_head()
        return f"migrated to {revision}"

    # Not managed: either brand new, or created by an earlier version of this
    # application that used `create_all` and nothing else. `create_all` is
    # idempotent, so both cases converge on "the current schema", after which the
    # stamp makes every future start take the migration branch.
    create_all()
    stamp_head()
    return f"created and stamped at {head_revision()}"


__all__ = [
    "VERSION_TABLE",
    "alembic_config",
    "current_revision",
    "ensure_schema",
    "head_revision",
    "head_revisions",
    "is_managed",
    "stamp_head",
    "upgrade_to_head",
]
