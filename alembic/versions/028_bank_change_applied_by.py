"""record who applied a vendor bank change

Revision ID: 028_bank_change_applied_by
Revises: 027_vendor_bank_changes
Create Date: 2026-08-18 00:00:00.000000

Build Book SoD rule: "Same person cannot change vendor bank details and approve
the first payment after change."

Enforcing that needs to know who changed them. The change record named the
requester and the approver but not whoever wrote the details onto the vendor,
even though `apply_change` is the moment the account actually changes — it
requires only vendors.manage, so it can legitimately be a third person, or the
requester themselves.

Nullable with no backfill: rows applied before this migration genuinely have no
recorded applier, and inventing one — the requester, say — would put a name in
the audit trail that nobody can stand behind.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '028_bank_change_applied_by'
down_revision = '027_vendor_bank_changes'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'vendor_bank_changes',
        sa.Column('applied_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
    )


def downgrade():
    op.drop_column('vendor_bank_changes', 'applied_by')
