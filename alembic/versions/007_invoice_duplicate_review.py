"""duplicate review fields on invoices

Revision ID: 007_invoice_duplicate_review
Revises: 006_conversation_messages_rls
Create Date: 2026-06-02 00:00:00.000000

Persist the fuzzy-duplicate detection result on the invoice so the soft warning
becomes an auditable, overridable gate (per MVP spec): when a likely duplicate
is found at upload, potential_duplicate_id points at the matched invoice and
duplicate_acknowledged stays False until a reviewer overrides it with a logged
reason. Approval is blocked while a duplicate is unacknowledged.
"""
from alembic import op
import sqlalchemy as sa

revision = '007_invoice_duplicate_review'
down_revision = '006_conversation_messages_rls'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'invoices',
        sa.Column('potential_duplicate_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        'invoices',
        sa.Column(
            'duplicate_acknowledged',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        'fk_invoices_potential_duplicate_id',
        'invoices',
        'invoices',
        ['potential_duplicate_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_invoices_potential_duplicate_id', 'invoices', type_='foreignkey')
    op.drop_column('invoices', 'duplicate_acknowledged')
    op.drop_column('invoices', 'potential_duplicate_id')
