"""run step logs

Revision ID: 9c2a41d07be3
Revises: fb130c6ef8d8
Create Date: 2026-09-16 16:30:00.000000

Adds `run_steps.logs`, the lines a node emitted with `ctx.log(...)`.

Before this the recorder had no branch for the `log` event kind and `StepRecord`
had no field to put one in, so a node's own explanation of what it did reached
the live event stream and was then dropped — `GET /runs/{id}/steps` returned
`logs: None` for every step of every run, and the same was true of the transcript.

A JSON column rather than a child table because the lines are only ever read as a
unit with the step that produced them, and a table would put a join in the path of
every timeline render.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

# Load-bearing, and not what `alembic init` produces. See the note in the
# previous migration: autogenerate emits `sqlmodel.sql.sqltypes.AutoString()` and
# Alembic's import detection does not notice the dependency, so the generated
# file fails at import with `NameError: name 'sqlmodel' is not defined`.
import sqlmodel
from alembic import op


revision: str = "9c2a41d07be3"
down_revision: Union[str, None] = "fb130c6ef8d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("run_steps", schema=None) as batch_op:
        batch_op.add_column(sa.Column("logs", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_steps", schema=None) as batch_op:
        batch_op.drop_column("logs")
