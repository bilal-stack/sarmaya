"""change watchlist alerts

Revision ID: 030_watchlist_alerts
Revises: 029_soft_deletes
Create Date: 2026-08-18 00:00:00.000000

Build Book differentiator: *vendor bank changes, master data edits, and policy
overrides trigger real-time alerts to a watchlist role.*

All three were already audited. An audit trail answers "what happened to this
record" for somebody who has already decided to look at that record, and none
of these three give anyone a reason to look — each changes where money goes, or
who may authorise sending it, without touching a single invoice.

Stored rather than only emailed: an alert nobody can list is an alert nobody
can prove they reviewed, and the acknowledgement is the evidence that somebody
did.

Tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '030_watchlist_alerts'
down_revision = '029_soft_deletes'
branch_labels = None
depends_on = None

TABLE = 'watchlist_alerts'


def upgrade():
    op.create_table(
        TABLE,
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('category', sa.String(50), nullable=False, index=True),
        sa.Column('severity', sa.String(20), nullable=False,
                  server_default='medium'),
        # Not a foreign key: alerts point at several different tables, and one
        # of them (a withdrawn vendor or policy) may be soft-deleted by the
        # time anyone reads the alert.
        sa.Column('object_type', sa.String(50), nullable=False),
        sa.Column('object_id', postgresql.UUID(as_uuid=True), nullable=False,
                  index=True),
        sa.Column('summary', sa.String(), nullable=False),
        sa.Column('detail', postgresql.JSONB(), nullable=True),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('acknowledged_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.Column('acknowledgement_note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
    )

    # The default view is "what has nobody looked at yet", newest first.
    op.create_index(
        'ix_watchlist_alerts_open', TABLE, ['tenant_id', 'created_at'],
        postgresql_where=sa.text('acknowledged_at IS NULL'),
    )

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


def downgrade():
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_insert ON {TABLE}")
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE}")
    op.drop_index('ix_watchlist_alerts_open', table_name=TABLE)
    op.drop_table(TABLE)
