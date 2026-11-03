"""invites and human requests

`invite_pending` keeps unopened invite links out of the candidate lists; `human_after_knockout`
lets recruiters filter rejected candidates who asked for a person. Existing conversations were
never pending, and the requests they already hold are copied from the state.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-03 23:51:10.029143
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "human_after_knockout", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )
        batch_op.add_column(
            sa.Column("invite_pending", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    _backfill()


def _backfill() -> None:
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.String),
        sa.column("status", sa.String),
        sa.column("state", sa.JSON),
        sa.column("human_after_knockout", sa.Boolean),
    )
    connection = op.get_bind()
    rejected = sa.select(conversations.c.id, conversations.c.state).where(
        conversations.c.status == "disqualified"
    )
    for row_id, state in connection.execute(rejected).all():
        state = json.loads(state) if isinstance(state, str) else (state or {})
        if state.get("human_after_knockout"):
            connection.execute(
                sa.update(conversations)
                .where(conversations.c.id == row_id)
                .values(human_after_knockout=True)
            )


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("invite_pending")
        batch_op.drop_column("human_after_knockout")
