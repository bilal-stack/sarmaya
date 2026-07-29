"""add evidence_packs (audit-ready bundles)

Revision ID: 017_evidence_packs
Revises: 016_correlation_id
Create Date: 2026-06-05 00:00:00.000000

Build Book: "Evidence Pack Generator: one-click audit-ready bundle with hashes,
logs, and policy snapshots" (canonical EvidencePack entity). Records that a pack
was generated for a transaction chain, by whom, with a SHA-256 seal over the
bundle and a manifest holding attachment hashes and the chain-verification
result at generation time.

Tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa

revision = '017_evidence_packs'
down_revision = '016_correlation_id'
branch_labels = None
depends_on = None

TABLE = 'evidence_packs'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('pack_hash', sa.String(64), nullable=False),
        sa.Column('manifest', sa.JSON(), nullable=False),
        sa.Column('generated_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['generated_by'], ['users.id']),
    )
    op.create_index('ix_evidence_packs_chain', TABLE, ['tenant_id', 'correlation_id'])

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
    op.drop_index('ix_evidence_packs_chain', table_name=TABLE)
    op.drop_table(TABLE)
