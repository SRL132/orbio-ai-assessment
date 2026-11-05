"""llm outputs

The LLM's raw replies, kept only if LLM_CAPTURE_OUTPUTS is set. Erased with the conversation.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-05 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_outputs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_outputs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_llm_outputs_conversation_id"), ["conversation_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_llm_outputs_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("llm_outputs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_outputs_created_at"))
        batch_op.drop_index(batch_op.f("ix_llm_outputs_conversation_id"))
    op.drop_table("llm_outputs")
