"""workflow version metadata for AI changes

Revision ID: c3f1a9b2d4e5
Revises: 2a9f4c17d8e3
Create Date: 2026-10-20 09:50:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c3f1a9b2d4e5"
down_revision: Union[str, None] = "2a9f4c17d8e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # JSON, nullable: rows written before this column existed have no record,
    # and an empty object would claim an AI change happened when it did not.
    with op.batch_alter_table("workflow_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workflow_versions", schema=None) as batch_op:
        batch_op.drop_column("meta")
