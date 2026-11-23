"""Schema migration guards.

What these tests are actually defending
---------------------------------------

The application reaches its schema by two routes: ``create_all()`` for a database
that does not exist yet (and for every test), and alembic for one that already
does. Two routes to one schema is a real hazard — they can drift, and the
symptom is a column that exists in development and not in production.

``TestTheTwoPathsAgree`` is the guard that makes the arrangement safe. It builds
a database each way and compares what came out. A model change whose migration
was forgotten, or a migration that no longer matches the models, fails here
rather than in a deployment.

This is also the ``check-schema-drift`` item that
``docs/code/task-13-postgres.md#t13b`` lists as the highest-value short-term
task — it just turned out to belong to the database schema rather than to the
TypeScript mirror.

A note on why the comparison is structural rather than textual
-------------------------------------------------------------

Comparing generated DDL strings would fail on cosmetic differences (index
creation order, whitespace) while missing a genuine one (a column whose type
changed but whose DDL happens to render the same length). Comparing
``Inspect``-derived structure — table names, columns with type and nullability,
indexes, foreign keys — catches what matters and ignores what does not.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import inspect

from app.db import migrate
from app.db.session import create_all, dispose_engines, get_engine
from app.settings import get_settings


def _structure(engine: Any) -> dict[str, Any]:
    """A comparable description of every table in a database.

    Excludes ``alembic_version``, which exists only on the migrated side and is
    alembic's bookkeeping rather than part of the application schema.
    """
    inspector = inspect(engine)
    described: dict[str, Any] = {}

    for table in sorted(inspector.get_table_names()):
        if table == migrate.VERSION_TABLE:
            continue
        described[table] = {
            "columns": sorted(
                (
                    column["name"],
                    # `str(type)` rather than the type object: two SQLAlchemy type
                    # instances built from the same declaration are not equal, and
                    # comparing them would fail for reasons that do not matter.
                    str(column["type"]),
                    bool(column["nullable"]),
                )
                for column in inspector.get_columns(table)
            ),
            "primary_key": sorted(
                inspector.get_pk_constraint(table).get("constrained_columns") or []
            ),
            "indexes": sorted(
                (
                    index["name"],
                    tuple(index.get("column_names") or ()),
                    bool(index.get("unique")),
                )
                for index in inspector.get_indexes(table)
            ),
            "foreign_keys": sorted(
                (
                    tuple(fk.get("constrained_columns") or ()),
                    fk.get("referred_table"),
                    tuple(fk.get("referred_columns") or ()),
                )
                for fk in inspector.get_foreign_keys(table)
            ),
        }
    return described


@pytest.fixture
def _fresh_data_dir(tmp_path, monkeypatch):
    """Point the app at a writable, empty data directory for this test only.

    The suite-wide fixture already does this, but these tests delete and rebuild
    the database file, so they need to know exactly where it is.
    """
    data_dir = tmp_path / "migration-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    get_settings.cache_clear()
    dispose_engines()
    yield data_dir
    get_settings.cache_clear()
    dispose_engines()


def _remove_database(data_dir) -> None:
    """Dispose every engine, then delete the SQLite file and its WAL sidecars.

    Order matters and is the reason this is a function rather than three lines at
    each call site: on Windows an open connection holds a mandatory lock, so
    deleting first fails with ``PermissionError: [WinError 32]``. Disposing first
    is silent when you get it right and a confusing failure when you do not, which
    is exactly the shape of thing that should live in one place.

    The ``-wal``/``-shm`` files matter too: leaving them behind lets the next
    connection recover a schema this test just deleted, producing a "fresh"
    database that is not fresh.
    """
    dispose_engines()
    for suffix in ("", "-wal", "-shm"):
        candidate = data_dir / f"scaffold.db{suffix}"
        if candidate.exists():
            candidate.unlink()


class TestTheTwoPathsAgree:
    """The guard. This is the reason both schema paths are allowed to exist."""

    def test_migrations_produce_the_same_schema_as_create_all(self, _fresh_data_dir):
        create_all()
        from_create_all = _structure(get_engine())

        _remove_database(_fresh_data_dir)
        migrate.upgrade_to_head()
        from_migrations = _structure(get_engine())

        assert from_migrations == from_create_all, (
            "The migrated schema and create_all() disagree. Either a model changed "
            "without a migration (`alembic revision --autogenerate -m '...'`), or a "
            "migration no longer matches the models."
        )

    def test_the_comparison_would_notice_a_missing_table(self, _fresh_data_dir):
        """Negative control for the comparison itself.

        A comparison that passes for any two schemas is worse than none, because
        it reports agreement that was never checked.
        """
        create_all()
        baseline = _structure(get_engine())

        with get_engine().connect() as connection:
            connection.exec_driver_sql("DROP TABLE run_steps")
            connection.commit()

        assert _structure(get_engine()) != baseline

    def test_the_comparison_would_notice_a_dropped_index(self, _fresh_data_dir):
        """Indexes are the easiest thing to lose and the hardest to notice.

        Without this control, deleting the ``indexes`` entry from ``_structure``
        would silently stop the drift test from checking indexes at all — and
        every test would still pass, because both sides would be equally
        ignorant. This is the same failure as an assertion that cannot fail: it
        looks like coverage and is not.
        """
        create_all()
        baseline = _structure(get_engine())

        with get_engine().connect() as connection:
            connection.exec_driver_sql("DROP INDEX ix_runs_status")
            connection.commit()

        assert _structure(get_engine()) != baseline

    def test_the_comparison_would_notice_an_added_column(self, _fresh_data_dir):
        """An added column is the canonical migration, so it must be visible.

        A real database change rather than a hand-edited description: mutating the
        dict in Python would test the comparison operator and nothing about
        ``_structure``, which is the part that can be wrong.
        """
        create_all()
        baseline = _structure(get_engine())

        with get_engine().connect() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE workflows ADD COLUMN scratch_marker VARCHAR"
            )
            connection.commit()

        assert _structure(get_engine()) != baseline


class TestMigrationsRunInBothDirections:
    def test_upgrade_creates_every_table(self, _fresh_data_dir):
        _remove_database(_fresh_data_dir)

        revision = migrate.upgrade_to_head()

        assert revision == migrate.head_revision()
        tables = set(inspect(get_engine()).get_table_names())
        assert {"workflows", "runs", "run_steps"} <= tables

    def test_the_database_becomes_managed(self, _fresh_data_dir):
        assert migrate.is_managed() is False

        migrate.upgrade_to_head()

        assert migrate.is_managed() is True
        assert migrate.current_revision() == migrate.head_revision()

    def test_downgrade_is_reversible(self, _fresh_data_dir):
        """A migration that cannot be reversed cannot be tested.

        Also the practical requirement for a rollback: if a deploy goes wrong, the
        previous release has to be able to run against the database it left behind.
        """
        _remove_database(_fresh_data_dir)
        migrate.upgrade_to_head()

        from alembic import command

        command.downgrade(migrate.alembic_config(), "base")

        tables = set(inspect(get_engine()).get_table_names())
        assert not ({"workflows", "runs", "run_steps"} & tables)

    def test_there_is_exactly_one_head(self, _fresh_data_dir):
        """Two heads mean the revision history diverged.

        Common when two migrations are authored on separate branches, annoying to
        diagnose at ``upgrade`` time, and trivial to catch here.
        """
        assert len(migrate.head_revisions()) == 1, migrate.head_revisions()


class TestEnsureSchemaAdoptsWhateverItFinds:
    """What the application actually calls at startup."""

    def test_a_brand_new_database_is_created_and_stamped(self, _fresh_data_dir):
        _remove_database(_fresh_data_dir)

        outcome = migrate.ensure_schema()

        assert "created" in outcome
        assert migrate.current_revision() == migrate.head_revision()

    def test_an_existing_create_all_database_is_adopted(self, _fresh_data_dir):
        """The upgrade path for every deployment that predates alembic.

        Without this branch, ``alembic upgrade`` against such a database would try
        to create tables that already exist and fail. Adoption stamps it instead.
        """
        create_all()
        assert migrate.is_managed() is False

        outcome = migrate.ensure_schema()

        assert "created" in outcome
        assert migrate.current_revision() == migrate.head_revision()
        # The tables are still there -- adoption must not rebuild anything.
        tables = set(inspect(get_engine()).get_table_names())
        assert {"workflows", "runs", "run_steps"} <= tables

    def test_adoption_preserves_existing_rows(self, _fresh_data_dir):
        """The assertion that matters for a deployment that already has data."""
        create_all()
        with get_engine().connect() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workflows (id, name, description, version, created_at, updated_at) "
                "VALUES ('wf-keep', 'Kept', '', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.commit()

        migrate.ensure_schema()

        with get_engine().connect() as connection:
            names = [
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM workflows WHERE id = 'wf-keep'"
                )
            ]
        assert names == ["Kept"]

    def test_a_managed_database_takes_the_migration_branch(self, _fresh_data_dir):
        _remove_database(_fresh_data_dir)
        migrate.ensure_schema()

        outcome = migrate.ensure_schema()

        assert outcome.startswith("migrated to"), outcome

    def test_running_twice_is_safe(self, _fresh_data_dir):
        """Startup happens on every reload; ensure_schema must be idempotent."""
        _remove_database(_fresh_data_dir)

        first = migrate.ensure_schema()
        second = migrate.ensure_schema()

        assert first and second
        assert migrate.current_revision() == migrate.head_revision()

    def test_it_reports_which_branch_it_took(self, _fresh_data_dir):
        """Startup logs this, and it is the first thing to check when a schema
        problem appears. Unanswerable after the fact if nothing records it."""
        _remove_database(_fresh_data_dir)

        assert migrate.ensure_schema().startswith("created")
        assert migrate.ensure_schema().startswith("migrated")
