"""withdraw records instead of destroying them

Revision ID: 029_soft_deletes
Revises: 028_bank_change_applied_by
Create Date: 2026-08-18 00:00:00.000000

Build Book non-negotiable: *immutable audit — guardrails to prevent hard
deletes*.

Deleting a vendor, a draft invoice or an approval policy removed the row while
the audit entry describing the deletion stayed behind, pointing at an id that
no longer resolved. The trail recorded that something happened to something the
database says never existed. For policies it was worse than a dangling
reference: `config_versions` keeps a snapshot per change including the deletion
itself, and the rollback this system offers could restore a version of a policy
whose row was gone.

Nothing is backfilled, because there is nothing to backfill — rows destroyed
before this migration are destroyed. `deleted_at` is indexed since every ORM
query now filters on it.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '029_soft_deletes'
down_revision = '028_bank_change_applied_by'
branch_labels = None
depends_on = None

TABLES = ('vendors', 'invoices', 'policies')


def upgrade():
    for table in TABLES:
        op.add_column(table, sa.Column('deleted_at', sa.DateTime(), nullable=True))
        op.add_column(
            table,
            sa.Column('deleted_by', postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.add_column(table, sa.Column('deletion_reason', sa.String(), nullable=True))
        op.create_index(
            f'ix_{table}_deleted_at', table, ['deleted_at'], unique=False
        )


def downgrade():
    for table in TABLES:
        op.drop_index(f'ix_{table}_deleted_at', table_name=table)
        op.drop_column(table, 'deletion_reason')
        op.drop_column(table, 'deleted_by')
        op.drop_column(table, 'deleted_at')
