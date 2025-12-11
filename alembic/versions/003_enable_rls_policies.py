"""enable RLS policies

Revision ID: 003_enable_rls_policies
Revises: 002_seed_demo_data
Create Date: 2024-01-15 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '003_enable_rls_policies'
down_revision = '002_seed_demo_data'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Enable RLS and create policies for all multi-tenant tables."""
    
    # List of tables that need RLS
    tables = [
        'users',
        'vendors',
        'workflow_states',
        'policies',
        'files',
        'invoices',
        'audit_logs',
        'conversations',
    ]
    
    conn = op.get_bind()
    
    for table in tables:
        # Enable RLS
        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"))
        
        # Create policy: users can only see rows for their tenant
        conn.execute(sa.text(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE));
        """))
        
        # Create policy for INSERT (use same tenant_id)
        conn.execute(sa.text(f"""
            CREATE POLICY {table}_tenant_insert ON {table}
            FOR INSERT
            WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE));
        """))


def downgrade() -> None:
    """Disable RLS and drop policies."""
    
    tables = [
        'users',
        'vendors',
        'workflow_states',
        'policies',
        'files',
        'invoices',
        'audit_logs',
        'conversations',
        'conversation_messages'
    ]
    
    conn = op.get_bind()
    
    for table in tables:
        # Drop policies
        conn.execute(sa.text(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};"))
        conn.execute(sa.text(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table};"))
        
        # Disable RLS
        conn.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;"))