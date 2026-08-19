"""multi-factor authentication

Revision ID: 034_mfa
Revises: 033_in_app_notifications
Create Date: 2026-08-19 00:00:00.000000

Build Book, Access Controls and Operational Security: MFA support.

Every other control in this system reasons about identities — segregation of
duties, maker-checker, approval limits, the bank-change rules. A stolen
password makes all of them wrong at once, silently, and leaves an audit trail
naming the victim as the person who did it.

Nobody is enrolled by this migration and nothing is required: `mfa_enabled`
defaults to false and enrolment is a deliberate act per user. Turning it into a
requirement is a policy decision for a tenant, not a schema change.

The secret is stored encrypted (app.core.mfa), so a database dump alone does
not hand over the second factor. Recovery codes are hashed like passwords for
the same reason, in their own table so that spending one is a fact with a
timestamp.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '034_mfa'
down_revision = '033_in_app_notifications'
branch_labels = None
depends_on = None

TABLE = 'mfa_recovery_codes'


def upgrade():
    op.add_column('users', sa.Column('mfa_secret', sa.String(255), nullable=True))
    op.add_column('users', sa.Column(
        'mfa_enabled', sa.Boolean(), nullable=False, server_default='false'
    ))
    op.add_column('users', sa.Column('mfa_confirmed_at', sa.DateTime(), nullable=True))
    # The last accepted TOTP timestep: what stops a code being replayed inside
    # the window it is still technically valid for.
    op.add_column('users', sa.Column('mfa_last_timestep', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column(
        'mfa_failed_attempts', sa.Integer(), nullable=False, server_default='0'
    ))

    op.create_table(
        TABLE,
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('code_hash', sa.String(255), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
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
    op.drop_table(TABLE)
    op.drop_column('users', 'mfa_failed_attempts')
    op.drop_column('users', 'mfa_last_timestep')
    op.drop_column('users', 'mfa_confirmed_at')
    op.drop_column('users', 'mfa_enabled')
    op.drop_column('users', 'mfa_secret')
