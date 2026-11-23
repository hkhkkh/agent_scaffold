"""schedule kinds and webhook tokens

Revision ID: 7e5a0c3d91b4
Revises: 4d81b2f6a05c
Create Date: 2026-09-17 10:20:00.000000

Adds `schedules.kind` and `schedules.token`, which turn the table from "a stored
time rule" into "a stored trigger" — the second kind being a webhook.

Two columns, not a new table. Everything a webhook needs beyond its firing
mechanism already exists on this row: the workflow it points at, the orphan check
for when that workflow is deleted, the enabled flag, the run history. A separate
table would be a second copy of the store, the API surface, and the reconcile
pass, and the three would drift.

Both columns are `server_default`-ed so the migration is safe on a database that
already has rows — an existing schedule becomes `kind='schedule'` with an empty
token, which is exactly what it was.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

# Load-bearing, and not what `alembic init` produces. Autogenerate emits
# `sqlmodel.sql.sqltypes.AutoString()` on some column types and Alembic's import
# detection does not notice the dependency, so the generated file fails at import
# with `NameError: name 'sqlmodel' is not defined`. Present for the same reason as
# in the four migrations before it.
import sqlmodel  # noqa: F401
from alembic import op


revision: str = "7e5a0c3d91b4"
down_revision: Union[str, None] = "4d81b2f6a05c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `batch_alter_table` because SQLite cannot `ALTER TABLE ... ADD COLUMN` with a
    # non-constant default; batch mode recreates the table and copies the rows.
    #
    # The indexes are created *outside* the batch block, matching the migration
    # that created this table. Inside it they would be built against the temporary
    # table batch mode uses, and SQLite would then drop them with it.
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(), nullable=False, server_default="schedule")
        )
        batch_op.add_column(
            sa.Column("token", sa.String(), nullable=False, server_default="")
        )
    op.create_index("ix_schedules_kind", "schedules", ["kind"])
    op.create_index("ix_schedules_token", "schedules", ["token"])


def downgrade() -> None:
    op.drop_index("ix_schedules_token", table_name="schedules")
    op.drop_index("ix_schedules_kind", table_name="schedules")
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_column("token")
        batch_op.drop_column("kind")
