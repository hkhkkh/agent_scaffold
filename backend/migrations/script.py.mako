"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

# Load-bearing, and not what `alembic init` produces.
#
# Every column in this project comes from a SQLModel field, so autogenerate emits
# `sqlmodel.sql.sqltypes.AutoString()` and friends. Alembic's own import
# detection does not notice that dependency and omits this line, which makes the
# generated migration fail at import with `NameError: name 'sqlmodel' is not
# defined` -- a failure that only shows up when the migration is actually run.
import sqlmodel
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
