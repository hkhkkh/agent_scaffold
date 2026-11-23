"""schedules

Revision ID: 4d81b2f6a05c
Revises: 9c2a41d07be3
Create Date: 2026-09-16 19:40:00.000000

Adds the `schedules` table: a stored trigger that starts ordinary runs.

Columns worth a note:

* `rule` is JSON rather than one column per field. The fields differ by
  frequency — a weekly rule has weekdays, a monthly one has a day of month — so
  columns would be nullable ones whose validity depends on a *different* column,
  which is a constraint SQLite will not enforce for us.
* `status` and `enabled` are indexed because the scheduler's per-tick query
  filters on both. A tick happens once a minute for the life of the process, so
  this is the one table in the schema where the read path is a loop.
* `next_run_at` is indexed and nullable. Nullable is load-bearing: `NULL` means
  "no future occurrence", which is how a rule that can never match again
  (`bymonthday=31` in February) is retired instead of being re-queried forever.
* `last_fired_at` and `last_started_at` are two columns, not one. The first is the
  moment the schedule was *supposed* to fire; the second is when a run was
  actually created for it. Catch-up walks from the first, so collapse them and
  the backlog behind a single failure is skipped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

# Load-bearing, and not what `alembic init` produces. Autogenerate emits
# `sqlmodel.sql.sqltypes.AutoString()` on some column types and Alembic's import
# detection does not notice the dependency, so the generated file fails at import
# with `NameError: name 'sqlmodel' is not defined`. Present for the same reason as
# in the three migrations before it.
import sqlmodel  # noqa: F401
from alembic import op
from app.db.models import UtcDateTime


revision: str = "4d81b2f6a05c"
down_revision: Union[str, None] = "9c2a41d07be3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("rule", sa.JSON(), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("status_reason", sa.String(), nullable=False, server_default=""),
        sa.Column("dtstart", UtcDateTime(), nullable=False),
        sa.Column("next_run_at", UtcDateTime(), nullable=True),
        sa.Column("last_fired_at", UtcDateTime(), nullable=True),
        sa.Column("last_started_at", UtcDateTime(), nullable=True),
        sa.Column("last_run_id", sa.String(), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
        sa.Column("missed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
    )
    op.create_index("ix_schedules_workflow_id", "schedules", ["workflow_id"])
    op.create_index("ix_schedules_enabled", "schedules", ["enabled"])
    op.create_index("ix_schedules_status", "schedules", ["status"])
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_schedules_next_run_at", table_name="schedules")
    op.drop_index("ix_schedules_status", table_name="schedules")
    op.drop_index("ix_schedules_enabled", table_name="schedules")
    op.drop_index("ix_schedules_workflow_id", table_name="schedules")
    op.drop_table("schedules")
