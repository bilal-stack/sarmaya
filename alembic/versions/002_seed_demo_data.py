"""Seed demo data placeholder (noop)

Revision ID: 002_seed_demo_data
Revises: 002_seed_demo
Create Date: 2025-11-28 10:05:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '002_seed_demo_data'
down_revision: str = '002_seed_demo'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op placeholder migration to ensure linear history.
    pass


def downgrade() -> None:
    pass
