"""add config_versions (append-only config history)

Revision ID: 011_config_versions
Revises: 010_audit_hash_chain
Create Date: 2026-06-05 00:00:00.000000

Editable config (approval policies, autopilot settings, workflow transitions)
was edited in place — the prior values were only recoverable as per-field deltas
in the audit log. This adds an append-only config_versions table that stores the
full post-change JSON snapshot under a monotonic version number per config
object, giving real version history ("versioned JSON per workflow") and the
basis for rollback. The live tables stay the current source of truth.

Tenant-scoped, so it gets the same ENABLE + FORCE RLS isolation/insert policies
as the other tenant tables (mirrors migration 006).
"""
from alembic import op
import sqlalchemy as sa

revision = '011_config_versions'
down_revision = '010_audit_hash_chain'
branch_labels = None
depends_on = None

TABLE = 'config_versions'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('config_type', sa.String(50), nullable=False),
        sa.Column('config_key', sa.String(255), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('change_action', sa.String(20), nullable=False),
        sa.Column('change_reason', sa.String(), nullable=True),
        sa.Column('changed_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['changed_by'], ['users.id']),
        sa.UniqueConstraint(
            'tenant_id', 'config_type', 'config_key', 'version',
            name='uq_config_versions_object_version',
        ),
    )
    op.create_index(
        'ix_config_versions_object',
        TABLE,
        ['tenant_id', 'config_type', 'config_key', 'version'],
    )

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
    op.drop_index('ix_config_versions_object', table_name=TABLE)
    op.drop_table(TABLE)
