"""event sourcing

The event log becomes the source of truth, so each conversation's events need a strict order:
`events.seq` becomes a position in the conversation, reserved from `last_event_seq`. Conversations
that already exist have no recorded changes, so each one gets a `state_changed` event holding its
whole current state, as the base that later changes build on.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-04 14:20:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONVERSATIONS = sa.table(
    "conversations",
    sa.column("id", sa.String),
    sa.column("client_id", sa.String),
    sa.column("state", sa.JSON),
    sa.column("last_event_seq", sa.Integer),
)
_EVENTS = sa.table(
    "events",
    sa.column("id", sa.String),
    sa.column("conversation_id", sa.String),
    sa.column("client_id", sa.String),
    sa.column("seq", sa.Integer),
    sa.column("type", sa.String),
    sa.column("data", sa.JSON),
    sa.column("created_at", sa.DateTime),
)


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("last_event_seq", sa.Integer(), server_default="0", nullable=False)
        )
    _backfill()
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_events_order", ["conversation_id", "seq"])


def _backfill() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)
    conversations = bind.execute(
        sa.select(_CONVERSATIONS.c.id, _CONVERSATIONS.c.client_id, _CONVERSATIONS.c.state)
    ).all()
    for conversation_id, client_id, state in conversations:
        event_ids = bind.scalars(
            sa.select(_EVENTS.c.id)
            .where(_EVENTS.c.conversation_id == conversation_id)
            .order_by(_EVENTS.c.created_at, _EVENTS.c.seq, _EVENTS.c.id)
        ).all()
        for position, event_id in enumerate(event_ids, 1):
            bind.execute(sa.update(_EVENTS).where(_EVENTS.c.id == event_id).values(seq=position))
        last = len(event_ids) + 1
        bind.execute(
            sa.insert(_EVENTS).values(
                id=str(uuid4()),
                conversation_id=conversation_id,
                client_id=client_id,
                seq=last,
                type="state_changed",
                data=state,
                created_at=now,
            )
        )
        bind.execute(
            sa.update(_CONVERSATIONS)
            .where(_CONVERSATIONS.c.id == conversation_id)
            .values(last_event_seq=last)
        )


def downgrade() -> None:
    op.execute(sa.delete(_EVENTS).where(_EVENTS.c.type == "state_changed"))
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("uq_events_order", type_="unique")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("last_event_seq")
