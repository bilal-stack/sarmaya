"""force row level security on tenant tables

Revision ID: 005_force_rls
Revises: 4e11f17a4cc8
Create Date: 2026-06-01 00:00:00.000000

Without FORCE, Postgres exempts a table's owner from its RLS policies, so the
ENABLE-only policies from 003 are silently bypassed whenever the app connects
as the table owner. FORCE makes the policies apply to the owner too (defense in
depth). Note: superusers and BYPASSRLS roles still bypass RLS regardless — the
app must connect as a non-superuser, non-BYPASSRLS role for this to take effect.
"""
from alembic import op
import sqlalchemy as sa

revision = '005_force_rls'
down_revision = '4e11f17a4cc8'
branch_labels = None
depends_on = None

TABLES = [
    'users',
    'vendors',
    'workflow_states',
    'policies',
    'files',
    'invoices',
    'audit_logs',
    'conversations',
]


def upgrade() -> None:
    conn = op.get_bind()
    for table in TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"))


def downgrade() -> None:
    conn = op.get_bind()
    for table in TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;"))
