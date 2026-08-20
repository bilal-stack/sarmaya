"""job run heartbeats, so the console can see a job that stopped

Revision ID: 037_job_runs
Revises: 036_org_unit_scopes
Create Date: 2026-08-19 00:00:00.000000

The Definition of Done asks for an admin console with an error monitor. The
config screens, audit viewer and job views already exist; what was missing is
the ability to answer "is anything silently not running".

That cannot be derived from the work queues. An outbox with nothing pending
looks exactly the same whether the dispatcher ran a second ago or died last
week, and the difference only surfaces when somebody is waiting on a message
that will never arrive. So each scheduled run writes a row, and a stale
timestamp becomes something an administrator can see rather than something a
CFO discovers.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '037_job_runs'
down_revision = '036_org_unit_scopes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('job_name', sa.String(50), nullable=False, index=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='ok'),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('items_processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
    )

    # The monitor's only read is "the latest run of this job for this tenant",
    # against a table one job appends to every minute. Without this it is a
    # scan that grows all day and is slowest exactly when the console is being
    # opened to find out why something is slow.
    op.create_index(
        'ix_job_runs_tenant_job_started', 'job_runs',
        ['tenant_id', 'job_name', sa.text('started_at DESC')],
    )

    op.execute("ALTER TABLE job_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE job_runs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY job_runs_tenant_isolation ON job_runs
        USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)
    op.execute("""
        CREATE POLICY job_runs_tenant_insert ON job_runs
        FOR INSERT
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)


def downgrade():
    op.execute("DROP POLICY IF EXISTS job_runs_tenant_insert ON job_runs")
    op.execute("DROP POLICY IF EXISTS job_runs_tenant_isolation ON job_runs")
    op.drop_index('ix_job_runs_tenant_job_started', table_name='job_runs')
    op.drop_table('job_runs')
