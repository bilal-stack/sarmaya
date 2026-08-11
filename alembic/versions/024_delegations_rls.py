"""enable row level security on delegations

Revision ID: 024_delegations_rls
Revises: 023_bank_statements
Create Date: 2026-08-12 00:00:00.000000

Every other tenant-owned table in this schema is created with ENABLE + FORCE RLS
and a pair of tenant-isolation policies. `delegations` was not — migration 018
creates the table and its index and stops there. Found by running the migrations
against an empty database and comparing pg_policies against the tables that
carry a tenant_id.

It matters more here than the omission suggests. A delegation is a grant of
someone else's approval authority: a row readable across tenants would let one
tenant's records name another tenant's users as delegates, and the application's
own scoping (DR-013) is the only thing that was standing in the way. RLS is the
second lock, and it was missing on the table describing who may act for whom.
"""
from alembic import op

revision = '024_delegations_rls'
down_revision = '023_bank_statements'
branch_labels = None
depends_on = None

TABLE = 'delegations'


def upgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {TABLE}_tenant_isolation ON {TABLE}
        USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)
    op.execute(f"""
        CREATE POLICY {TABLE}_tenant_insert ON {TABLE}
        FOR INSERT
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_insert ON {TABLE}")
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
