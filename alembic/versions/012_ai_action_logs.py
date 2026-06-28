"""add ai_action_logs (AI invocation audit trail)

Revision ID: 012_ai_action_logs
Revises: 011_config_versions
Create Date: 2026-06-05 00:00:00.000000

The Build Book requires every AI action to be logged (model/provider, prompt
version, confidence, latency, status — Appendix A: ai.requested/completed/
failed_schema/hitl_requested). This adds an append-only ai_action_logs table,
tenant-scoped with the same ENABLE + FORCE RLS isolation as the other tenant
tables (mirrors migration 006/011).
"""
from alembic import op
import sqlalchemy as sa

revision = '012_ai_action_logs'
down_revision = '011_config_versions'
branch_labels = None
depends_on = None

TABLE = 'ai_action_logs'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('ai_provider', sa.String(50), nullable=True),
        sa.Column('ai_model', sa.String(100), nullable=True),
        sa.Column('prompt_version', sa.String(50), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('input_summary', sa.String(), nullable=True),
        sa.Column('output_summary', sa.String(), nullable=True),
        sa.Column('object_type', sa.String(50), nullable=True),
        sa.Column('object_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
    )
    op.create_index('ix_ai_action_logs_tenant', TABLE, ['tenant_id', 'action', 'created_at'])

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
    op.drop_index('ix_ai_action_logs_tenant', table_name=TABLE)
    op.drop_table(TABLE)
