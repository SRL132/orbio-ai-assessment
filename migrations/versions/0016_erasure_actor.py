"""erasure actor

Who erased a candidate on request; empty for the retention job.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-04 14:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("erasures", schema=None) as batch_op:
        batch_op.add_column(sa.Column("erased_by", sa.String(length=320), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("erasures", schema=None) as batch_op:
        batch_op.drop_column("erased_by")
