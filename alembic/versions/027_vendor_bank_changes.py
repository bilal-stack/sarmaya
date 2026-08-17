"""add controlled vendor bank detail changes

Revision ID: 027_vendor_bank_changes
Revises: 026_backfill_new_workflows
Create Date: 2026-08-14 00:00:00.000000

Build Book A1 control: vendor bank change verification with dual approval and
cooling period policy.

Before this, `PATCH /vendors/{id}` wrote bank fields directly behind
vendors.manage — which the AP clerk holds — and the audit entry for a vendor
update recorded only the legal name. So redirecting a vendor's payments was
uncontrolled *and* invisible, and every downstream control would still pass: a
genuine invoice, genuinely approved, genuinely released, paid to the wrong
account.

The old values are stored alongside the new because the substitution is what a
reviewer needs to see, and once the change is applied the vendor row no longer
holds the account it replaced.

Tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa

revision = '027_vendor_bank_changes'
down_revision = '026_backfill_new_workflows'
branch_labels = None
depends_on = None

UTC_NOW = sa.text("timezone('utc', now())")
TABLE = 'vendor_bank_changes'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        # How the change arrived and how it was verified. Required: it is what
        # the approver is judging, and "they emailed us" is a different answer
        # from "we rang the number we already had".
        sa.Column('reason', sa.String(), nullable=False),

        sa.Column('new_bank_account_name', sa.String(255), nullable=True),
        sa.Column('new_bank_account_number', sa.String(100), nullable=True),
        sa.Column('new_bank_name', sa.String(255), nullable=True),
        sa.Column('new_iban', sa.String(50), nullable=True),
        sa.Column('new_swift_code', sa.String(20), nullable=True),

        sa.Column('old_bank_account_name', sa.String(255), nullable=True),
        sa.Column('old_bank_account_number', sa.String(100), nullable=True),
        sa.Column('old_bank_name', sa.String(255), nullable=True),
        sa.Column('old_iban', sa.String(50), nullable=True),
        sa.Column('old_swift_code', sa.String(20), nullable=True),

        # Stores the enum member name, because that is what SQLAlchemy's
        # Enum type writes — it translates the lowercase values the Python
        # code uses on the way in and out. Writing the default or the index
        # predicate in lowercase silently matches nothing.
        sa.Column('current_state', sa.String(30), nullable=False,
                  server_default='PENDING_APPROVAL'),

        sa.Column('requested_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('requested_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('approved_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        # When the new details may first be paid to. Approval starts this
        # clock; until it passes, payments to the vendor are held.
        sa.Column('effective_at', sa.DateTime(), nullable=True),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('rejection_reason', sa.String(), nullable=True),

        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),

        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
    )
    op.create_index(f'ix_{TABLE}_tenant_id', TABLE, ['tenant_id'])
    op.create_index(f'ix_{TABLE}_vendor_id', TABLE, ['vendor_id'])

    # One unresolved change per vendor. Two open requests make it unclear which
    # account was agreed to, and an approver reading either has no way to tell
    # whether the other is the real one.
    op.create_index(
        f'uq_{TABLE}_one_open_per_vendor',
        TABLE,
        ['vendor_id'],
        unique=True,
        postgresql_where=sa.text(
            "current_state IN ('PENDING_APPROVAL', 'APPROVED')"
        ),
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


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_insert ON {TABLE}")
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE}")
    op.drop_table(TABLE)
