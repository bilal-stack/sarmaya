"""add delegations (temporary approval authority)

Revision ID: 018_delegations
Revises: 017_evidence_packs
Create Date: 2026-06-05 00:00:00.000000

Build Book: "Delegation supports temporary assignment of approvals and tasks
with start and end dates." A delegation lends the delegator's role authority to
the delegate for a bounded window without changing either user's own role.

Tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa

revision = '018_delegations'
down_revision = '017_evidence_packs'
branch_labels = None
depends_on = None

TABLE = 'delegations'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('from_user_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('to_user_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('starts_at', sa.DateTime(), nullable=False),
        sa.Column('ends_at', sa.DateTime(), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['from_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['to_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
    )
    op.create_index('ix_delegations_active', TABLE, ['tenant_id', 'to_user_id', 'ends_at'])


def downgrade() -> None:
    op.drop_index('ix_delegations_active', table_name=TABLE)
    op.drop_table(TABLE)
