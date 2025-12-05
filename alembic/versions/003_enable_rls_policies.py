"""Enable Row Level Security (RLS) policies

Revision ID: 003_enable_rls
Revises: 002_seed_demo_data
Create Date: 2025-11-28 11:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '003_enable_rls'
down_revision: str = '002_seed_demo_data'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable RLS on all tenant-scoped tables
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE vendors ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE files ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE workflow_states ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE policies ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;")
    
    # Create RLS policies for users table
    op.execute("""
        CREATE POLICY users_tenant_isolation ON users
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)
    
    # Create RLS policies for vendors table
    op.execute("""
        CREATE POLICY vendors_tenant_isolation ON vendors
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)
    
    # Create RLS policies for invoices table
    op.execute("""
        CREATE POLICY invoices_tenant_isolation ON invoices
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)
    
    # Create RLS policies for files table
    op.execute("""
        CREATE POLICY files_tenant_isolation ON files
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)
    
    # Create RLS policies for workflow_states table
    op.execute("""
        CREATE POLICY workflow_states_tenant_isolation ON workflow_states
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)
    
    # Create RLS policies for policies table
    op.execute("""
        CREATE POLICY policies_tenant_isolation ON policies
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)
    
    # Create RLS policies for audit_logs table
    op.execute("""
        CREATE POLICY audit_logs_tenant_isolation ON audit_logs
        USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));
    """)


def downgrade() -> None:
    # Drop policies
    op.execute("DROP POLICY IF EXISTS users_tenant_isolation ON users;")
    op.execute("DROP POLICY IF EXISTS vendors_tenant_isolation ON vendors;")
    op.execute("DROP POLICY IF EXISTS invoices_tenant_isolation ON invoices;")
    op.execute("DROP POLICY IF EXISTS files_tenant_isolation ON files;")
    op.execute("DROP POLICY IF EXISTS workflow_states_tenant_isolation ON workflow_states;")
    op.execute("DROP POLICY IF EXISTS policies_tenant_isolation ON policies;")
    op.execute("DROP POLICY IF EXISTS audit_logs_tenant_isolation ON audit_logs;")
    
    # Disable RLS
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE vendors DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE invoices DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE files DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE workflow_states DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE policies DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE audit_logs DISABLE ROW LEVEL SECURITY;")
