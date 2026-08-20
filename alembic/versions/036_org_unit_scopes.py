"""org units, and the scopes a role is exercised within

Revision ID: 036_org_unit_scopes
Revises: 035_dashboard_indexes
Create Date: 2026-08-19 00:00:00.000000

Build Book, Access Controls: *"RBAC with scopes: tenant, business unit,
location, cost center, project."* Until now a role was tenant-wide — a manager
who runs one warehouse approved invoices for every site, and an auditor
attached to one business unit read the whole company. Permissions said what
somebody may *do*; nothing said what they may do it *to*.

Nothing changes for anybody until units exist and are assigned. A user with no
rows in `user_org_scopes` sees the entire tenant, exactly as before, and a
record with a null `org_unit_id` stays visible to everyone. Both defaults are
deliberate: the alternative — where creating these tables silently hides every
record from every user — is a change discovered by a CFO rather than by a test.

Adding this now rather than later is the point. It changes the shape of the
data, and every invoice raised before it exists is one somebody has to decide
the owner of retroactively.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '036_org_unit_scopes'
down_revision = '035_dashboard_indexes'
branch_labels = None
depends_on = None

SCOPED_TABLES = ('invoices', 'purchase_requisitions', 'purchase_orders')


def upgrade():
    op.create_table(
        'org_units',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('code', sa.String(50), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('unit_type', sa.String(30), nullable=False, index=True),
        # Self-referencing: business unit -> location -> cost centre. RESTRICT
        # rather than CASCADE, because deleting a parent should not silently
        # take a subtree of live cost centres with it.
        sa.Column('parent_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id', ondelete='RESTRICT'),
                  nullable=True, index=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.UniqueConstraint('tenant_id', 'code', name='uq_org_units_tenant_code'),
    )

    op.create_table(
        'user_org_scopes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.UniqueConstraint('user_id', 'org_unit_id',
                            name='uq_user_org_scopes_user_unit'),
    )

    for table in SCOPED_TABLES:
        op.add_column(table, sa.Column(
            'org_unit_id', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('org_units.id'), nullable=True,
        ))
        op.create_index(f'ix_{table}_org_unit_id', table, ['org_unit_id'])

    for table in ('org_units', 'user_org_scopes'):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
        """)
        op.execute(f"""
            CREATE POLICY {table}_tenant_insert ON {table}
            FOR INSERT
            WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
        """)


def downgrade():
    for table in SCOPED_TABLES:
        op.drop_index(f'ix_{table}_org_unit_id', table_name=table)
        op.drop_column(table, 'org_unit_id')
    for table in ('user_org_scopes', 'org_units'):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.drop_table(table)
