"""workflow version history

Revision ID: 2a9f4c17d8e3
Revises: 7e5a0c3d91b4
Create Date: 2026-09-18 10:40:00.000000

Adds `workflow_versions` and `workflows.definition_hash`.

What this is for
================

Task 28: **a definition must stay runnable after the canvas moves on.** Saving a
workflow records an immutable snapshot of the whole graph, so a run from six months
ago can be reproduced from the definition it actually used rather than from
whatever is on the canvas today.

Two design points the schema encodes
------------------------------------

* **A separate table, not a `version` column on `workflows`.** A version column
  forces either a composite primary key — which breaks every existing query — or a
  "which row is current?" flag that every read would have to filter on. A history
  table leaves `workflows` exactly as it was: one row per workflow, still the
  current one. This is also what SQLAlchemy's own `examples/versioned_history`
  does.
* **Snapshots, not deltas.** Each row holds the full graph. A delta chain is
  smaller, but reconstructing one version requires replaying every earlier one, so
  a single bad row breaks all later versions. At tens of KB per definition the
  storage saving is not worth that failure mode.

`workflows.definition_hash` is nullable on purpose: rows that predate this
migration have no recorded hash, and backfilling one now would assert that the
definition was pinned at a moment when nobody pinned it. `NULL` means "unknown",
which is the truth.

The unique constraint on `(workflow_id, version)` is what makes the synthetic
integer primary key safe. Without it a retried insert could create two rows with
the same version number, and `get(id, version=N)` would return whichever the
database happened to order first.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

# Load-bearing, and not what `alembic init` produces. Autogenerate emits
# `sqlmodel.sql.sqltypes.AutoString()` on some column types and Alembic's import
# detection does not notice the dependency, so the generated file fails at import
# with `NameError: name 'sqlmodel' is not defined`. Present for the same reason as
# in the six migrations before it.
import sqlmodel  # noqa: F401
from alembic import op
from app.db.models import UtcDateTime


revision: str = "2a9f4c17d8e3"
down_revision: Union[str, None] = "7e5a0c3d91b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `batch_alter_table` for the added column: SQLite cannot `ALTER TABLE ADD
    # COLUMN` with a non-constant default, and batch mode recreates the table.
    #
    # The index is created outside the batch block, matching the migration that
    # added the trigger columns — inside it, the index would be built against the
    # temporary table batch mode uses and dropped along with it.
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.add_column(sa.Column("definition_hash", sa.String(), nullable=True))
    op.create_index("ix_workflows_definition_hash", "workflows", ["definition_hash"])

    op.create_table(
        "workflow_versions",
        # Synthetic key; `(workflow_id, version)` below is the real identity.
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("graph", sa.JSON(), nullable=True),
        sa.Column("saved_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),
    )
    op.create_index("ix_workflow_versions_workflow_id", "workflow_versions", ["workflow_id"])
    op.create_index("ix_workflow_versions_version", "workflow_versions", ["version"])
    op.create_index(
        "ix_workflow_versions_definition_hash", "workflow_versions", ["definition_hash"]
    )
    op.create_index("ix_workflow_versions_created_at", "workflow_versions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_versions_created_at", table_name="workflow_versions")
    op.drop_index("ix_workflow_versions_definition_hash", table_name="workflow_versions")
    op.drop_index("ix_workflow_versions_version", table_name="workflow_versions")
    op.drop_index("ix_workflow_versions_workflow_id", table_name="workflow_versions")
    op.drop_table("workflow_versions")

    op.drop_index("ix_workflows_definition_hash", table_name="workflows")
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.drop_column("definition_hash")
