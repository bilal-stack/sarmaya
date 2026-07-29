"""add policy_evals (policy evaluation snapshots)

Revision ID: 015_policy_evals
Revises: 014_workflow_sla
Create Date: 2026-06-05 00:00:00.000000

Build Book: "Every policy evaluation is stored with policy_version, inputs
snapshot, output decision, and reasons" (canonical PolicyEval entity). Routing
reasons were previously only embedded in the audit event's after_value, which
records the explanation but not the rule version or the inputs it ran on.

Tenant-scoped with the same ENABLE + FORCE RLS isolation as the other tenant
tables (mirrors migrations 006/011/012).
"""
from alembic import op
import sqlalchemy as sa

revision = '015_policy_evals'
down_revision = '014_workflow_sla'
branch_labels = None
depends_on = None

TABLE = 'policy_evals'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('policy_key', sa.String(100), nullable=False),
        sa.Column('policy_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('policy_name', sa.String(255), nullable=True),
        sa.Column('policy_version', sa.Integer(), nullable=True),
        sa.Column('inputs', sa.JSON(), nullable=False),
        sa.Column('output', sa.JSON(), nullable=False),
        sa.Column('reasons', sa.JSON(), nullable=True),
        sa.Column('object_type', sa.String(50), nullable=True),
        sa.Column('object_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('evaluated_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['evaluated_by'], ['users.id']),
    )
    op.create_index('ix_policy_evals_object', TABLE, ['tenant_id', 'object_type', 'object_id'])

    conn = op.get_bind()
    conn.execute(sa.text(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;"))
    conn.execute(sa.text(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;"))
    conn.execute(sa.text(f"""
        CREATE POLICY {TABLE}_tenant_isolation ON {TABLE}
        USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE));
    """))
    conn.execute(sa.text(f"""
        CREATE POLICY {TABLE}_tenant_insert ON {TABLE}
        FOR INSERT
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE));
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(f"DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE};"))
    conn.execute(sa.text(f"DROP POLICY IF EXISTS {TABLE}_tenant_insert ON {TABLE};"))
    op.drop_index('ix_policy_evals_object', table_name=TABLE)
    op.drop_table(TABLE)
