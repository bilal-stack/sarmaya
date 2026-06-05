"""add token_version to users (JWT revocation)

Revision ID: 009_user_token_version
Revises: 008_seed_config_policies
Create Date: 2026-06-05 00:00:00.000000

Stateless JWTs cannot be revoked on their own, so logout was previously a no-op
(the client just dropped the token, but a copied token stayed valid until expiry).

This adds a per-user `token_version` counter. It is embedded as a claim in every
issued token; get_current_user compares the token's claim against the live row,
and logout / change-password increment it, invalidating every token issued before
the bump on its next request. No new table and no extra query — get_current_user
already loads the user row each request.
"""
from alembic import op
import sqlalchemy as sa

revision = '009_user_token_version'
down_revision = '008_seed_config_policies'
branch_labels = None
depends_on = None

TABLE = 'users'
COLUMN = 'token_version'


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)
