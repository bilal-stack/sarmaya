"""queue notifications instead of sending them inside the request

Revision ID: 032_notification_outbox
Revises: 031_rfq_award_sla
Create Date: 2026-08-19 00:00:00.000000

Every notification was sent synchronously inside the request that triggered it,
with the exception swallowed. So an approval waited on a mail server before the
user got a response, and when delivery failed nobody learned — the audit trail
recorded that an SLA breach had been escalated to the CFO while no message was
ever sent.

A row here is written in the *same transaction* as the action it describes,
which is the property that makes this a table rather than a thread: an approval
that rolls back queues nothing, and one that commits queues for certain. The
dispatcher drains it afterwards, records failures with their error, and retries
on a backoff.

Tenant-scoped with the usual ENABLE + FORCE RLS isolation. The bodies contain
whatever the notification said, which for an escalation names the record and
its amount, so this table is as tenant-private as the records it describes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '032_notification_outbox'
down_revision = '031_rfq_award_sla'
branch_labels = None
depends_on = None

TABLE = 'notification_outbox'


def upgrade():
    op.create_table(
        TABLE,
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('to_email', sa.String(255), nullable=False),
        sa.Column('subject', sa.String(500), nullable=False),
        sa.Column('body', sa.String(), nullable=False),
        sa.Column('category', sa.String(50), nullable=True, index=True),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='pending', index=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
    )

    # The drain's only query: what is due, oldest first.
    op.create_index(
        'ix_notification_outbox_due', TABLE, ['status', 'next_attempt_at'],
        postgresql_where=sa.text("status = 'pending'"),
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
    op.drop_index('ix_notification_outbox_due', table_name=TABLE)
    op.drop_table(TABLE)
