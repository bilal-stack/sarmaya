"""add_line_items_to_invoices

Revision ID: 4e11f17a4cc8
Revises: 003_enable_rls_policies
Create Date: 2025-12-13 10:25:57.229385

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e11f17a4cc8'
down_revision: Union[str, None] = '003_enable_rls_policies'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns(table)]
    return column in cols


def upgrade() -> None:
    # The initial schema (9ee83d7f931f) already creates invoices.line_items, so
    # only add it here for databases that predate that and stop at revision 003.
    if not _has_column("invoices", "line_items"):
        op.add_column('invoices', sa.Column('line_items', sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("invoices", "line_items"):
        op.drop_column('invoices', 'line_items')
