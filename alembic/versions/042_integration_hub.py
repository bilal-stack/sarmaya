"""the integration hub: a tenant's own accounting system, connected

Revision ID: 042_integration_hub
Revises: 041_hr
Create Date: 2026-08-20 00:00:00.000000

Build Book: "Integration connectors implemented with retries, idempotency,
dead-letter queues, and reconciliation records." Confirmed unbuilt before this
migration — no reference to "integration", "webhook" or "external_id" existed
anywhere in the codebase.

QuickBooks Online is the first provider; the model deliberately does not
assume it is the only one — `provider` is a string column, not an enum, so a
second provider is a new value rather than a migration (the same reasoning
`JobRun.job_name` already gives).

Two things this migration does NOT set up, on purpose: no two-way sync tables
(the pulled data is delete-and-replace, never merged or reconciled against
edits made in Sarmaya — there is nothing to reconcile because Sarmaya never
edits a QuickBooks record), and no WORKFLOW_TYPE / SLA states for the
connection or the queue (their status is read by the scheduled health job, not
by the SLA engine — declaring one without the other is exactly the invisible
gap DR-048 already found twice this project).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '042_integration_hub'
down_revision = '041_hr'
branch_labels = None
depends_on = None

#: Every new table is tenant-scoped and gets the same RLS treatment as the
#: rest of the schema. Listed once so none can be missed — a credentials
#: table without RLS would be a much worse omission here than usual.
NEW_TABLES = (
    'integration_connections',
    'integration_account_snapshots',
    'integration_party_snapshots',
    'integration_vendor_mappings',
    'integration_journal_posts',
)


def _timestamps():
    return (
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
    )


def _tenant_fk():
    return sa.Column(
        'tenant_id', postgresql.UUID(as_uuid=True),
        sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
        index=True,
    )


def upgrade():
    op.create_table(
        'integration_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('provider', sa.String(30), nullable=False, index=True),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='not_connected', index=True),
        sa.Column('external_company_id', sa.String(100), nullable=True),
        sa.Column('external_company_name', sa.String(255), nullable=True),
        sa.Column('access_token_encrypted', sa.String(), nullable=True),
        sa.Column('refresh_token_encrypted', sa.String(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(), nullable=True),
        sa.Column('refresh_token_expires_at', sa.DateTime(), nullable=True),
        sa.Column('oauth_state', sa.String(128), nullable=True, index=True),
        sa.Column('oauth_state_expires_at', sa.DateTime(), nullable=True),
        sa.Column('oauth_initiated_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('connected_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('connected_at', sa.DateTime(), nullable=True),
        sa.Column('disconnected_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('disconnected_at', sa.DateTime(), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'provider',
                            name='uq_integration_connections_tenant_provider'),
    )

    op.create_table(
        'integration_account_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('integration_connections.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('external_account_id', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('account_type', sa.String(50), nullable=True, index=True),
        sa.Column('account_sub_type', sa.String(50), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint('connection_id', 'external_account_id',
                            name='uq_account_snapshot_connection_external'),
    )

    op.create_table(
        'integration_party_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('integration_connections.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('external_party_id', sa.String(100), nullable=False),
        sa.Column('party_type', sa.String(20), nullable=False, index=True),
        sa.Column('display_name', sa.String(255), nullable=False, index=True),
        sa.Column('email', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint('connection_id', 'external_party_id', 'party_type',
                            name='uq_party_snapshot_connection_external'),
    )

    op.create_table(
        'integration_vendor_mappings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('integration_connections.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('vendors.id'), nullable=False, index=True),
        sa.Column('external_party_id', sa.String(100), nullable=False),
        sa.Column('mapped_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('mapped_at', sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint('connection_id', 'vendor_id',
                            name='uq_vendor_mapping_connection_vendor'),
    )

    op.create_table(
        'integration_journal_posts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('integration_connections.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('source_type', sa.String(30), nullable=False, index=True),
        sa.Column('source_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='pending', index=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('posted_at', sa.DateTime(), nullable=True),
        sa.Column('external_transaction_id', sa.String(100), nullable=True),
        sa.Column('external_transaction_type', sa.String(50), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint('connection_id', 'source_type', 'source_id',
                            name='uq_journal_post_connection_source'),
    )
    # The dispatcher's only read is "due rows", against a table one payment or
    # expense-payout appends to. Without this it is a scan that grows all day
    # and is slowest exactly when the queue is backed up.
    op.create_index(
        'ix_integration_journal_posts_due', 'integration_journal_posts',
        ['status', 'next_attempt_at'],
        postgresql_where=sa.text("status = 'pending'"),
    )

    for table in NEW_TABLES:
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
    for table in NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    op.drop_index('ix_integration_journal_posts_due', table_name='integration_journal_posts')
    op.drop_table('integration_journal_posts')
    op.drop_table('integration_vendor_mappings')
    op.drop_table('integration_party_snapshots')
    op.drop_table('integration_account_snapshots')
    op.drop_table('integration_connections')
