"""which accounts a posted journal entry actually debits and credits

Revision ID: 043_integration_default_accounts
Revises: 042_integration_hub
Create Date: 2026-08-20 00:00:00.000000

Migration 042 built the connection, the pull, and the queue, but left no way
to say which of the pulled accounts a posted entry should use. That is not a
detail to fill in later — `post_journal_entry` cannot build a line without an
`AccountRef`, so without this a connected tenant's queue drains into nothing
but failures.

Set once by the admin from the chart of accounts already pulled, not guessed
by matching a name like "Accounts Payable" — a chart that spells it
differently, or has more than one such account, would make a guess silently
wrong in a way nobody notices until a reconciliation doesn't tie out.

Nullable, and deliberately so: a connection with these unset is treated by
`JournalPostingService.enqueue` exactly like no connection at all — posting is
opportunistic, and a tenant who connected but has not yet configured which
accounts to use must not have payments blocked or warned on release.
"""
from alembic import op
import sqlalchemy as sa

revision = '043_integration_default_accounts'
down_revision = '042_integration_hub'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'integration_connections',
        sa.Column('default_liability_account_external_id', sa.String(100), nullable=True),
    )
    op.add_column(
        'integration_connections',
        sa.Column('default_bank_account_external_id', sa.String(100), nullable=True),
    )


def downgrade():
    op.drop_column('integration_connections', 'default_bank_account_external_id')
    op.drop_column('integration_connections', 'default_liability_account_external_id')
