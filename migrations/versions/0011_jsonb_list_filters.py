"""jsonb list filters

On Postgres the availability and schedule columns become JSONB with GIN indexes, so the
dashboard's list filters (`@>`) use an index. SQLite keeps JSON with plain indexes.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 23:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("availability", "schedule")


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for column in _COLUMNS:
        if postgres:
            op.execute(
                f"ALTER TABLE conversations ALTER COLUMN {column} TYPE JSONB USING {column}::jsonb"
            )
        op.create_index(
            f"ix_conversations_{column}",
            "conversations",
            [column],
            postgresql_using="gin",
        )


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for column in _COLUMNS:
        op.drop_index(f"ix_conversations_{column}", table_name="conversations")
        if postgres:
            op.execute(
                f"ALTER TABLE conversations ALTER COLUMN {column} TYPE JSON USING {column}::json"
            )
