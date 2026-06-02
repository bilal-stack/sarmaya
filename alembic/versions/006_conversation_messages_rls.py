"""tenant_id + RLS on conversation_messages

Revision ID: 006_conversation_messages_rls
Revises: 005_force_rls
Create Date: 2026-06-02 00:00:00.000000

conversation_messages was the only tenant-scoped table without a tenant_id
column or RLS policy. Isolation was enforced only at the application layer via
parent-conversation ownership checks, which is fragile: get_conversation_history
queries messages directly. This adds the column (backfilled from the parent
conversation), makes it NOT NULL, and applies ENABLE + FORCE RLS with the same
isolation/insert policies used on the other tenant tables.
"""
from alembic import op
import sqlalchemy as sa

revision = '006_conversation_messages_rls'
down_revision = '005_force_rls'
branch_labels = None
depends_on = None

TABLE = 'conversation_messages'


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        TABLE,
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )

    # Backfill from the parent conversation.
    conn.execute(sa.text(f"""
        UPDATE {TABLE} AS m
        SET tenant_id = c.tenant_id
        FROM conversations AS c
        WHERE m.conversation_id = c.id;
    """))

    op.alter_column(TABLE, 'tenant_id', nullable=False)
    op.create_foreign_key(
        f'fk_{TABLE}_tenant_id',
        TABLE,
        'tenants',
        ['tenant_id'],
        ['id'],
    )

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
    conn.execute(sa.text(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY;"))
    conn.execute(sa.text(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY;"))
    op.drop_constraint(f'fk_{TABLE}_tenant_id', TABLE, type_='foreignkey')
    op.drop_column(TABLE, 'tenant_id')
