"""client-scoped unique constraints

Interview slots and WhatsApp contacts were unique across all clients: two clients with the same
location id blocked each other's slots, and one phone number could only apply to one client.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLite constraints made by 0001/0002 have no name; batch mode reflects them under this one.
_NAMING = {"uq": "uq_%(table_name)s_%(column_0_N_name)s"}

_CHANGES = [
    ("interviews", ["location_id", "starts_at"], "uq_interviews_slot"),
    ("channel_contacts", ["channel", "external_id"], "uq_channel_contacts_contact"),
]


def _unique_name(table: str, columns: list[str]) -> str:
    """The existing constraint's name: Postgres generated one, SQLite left it unnamed."""
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table):
        if constraint["column_names"] == columns:
            return constraint["name"] or f"uq_{table}_{'_'.join(columns)}"
    raise RuntimeError(f"no unique constraint on {table}({', '.join(columns)})")


def upgrade() -> None:
    for table, columns, new_name in _CHANGES:
        old_name = _unique_name(table, columns)
        with op.batch_alter_table(table, naming_convention=_NAMING) as batch_op:
            batch_op.drop_constraint(old_name, type_="unique")
            batch_op.create_unique_constraint(new_name, ["client_id", *columns])


def downgrade() -> None:
    for table, columns, new_name in _CHANGES:
        with op.batch_alter_table(table, naming_convention=_NAMING) as batch_op:
            batch_op.drop_constraint(new_name, type_="unique")
            batch_op.create_unique_constraint(f"uq_{table}_{'_'.join(columns)}", columns)
