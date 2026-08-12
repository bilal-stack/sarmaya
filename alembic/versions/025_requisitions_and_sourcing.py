"""add requisitions, RFQs and quotes

Revision ID: 025_requisitions_and_sourcing
Revises: 024_delegations_rls
Create Date: 2026-08-12 00:00:00.000000

The upstream half of procure-to-pay. Until now the purchase order was the first
record in the chain, so an approver had nothing to check it against and the
audit trail could prove an order was properly approved without answering why it
was ordered at all.

`purchase_requisitions` is that missing record — who asked, for what, why —
and it now mints the correlation id that the RFQ, the quotes, the order, the
receipts, the invoice and the payment all inherit.

`rfqs` / `rfq_vendors` / `quotes` / `quote_lines` hold the sourcing decision.
Invited vendors are stored separately from quotes on purpose: a vendor who was
asked and did not answer is part of the record, because an award is only
competitive if the invitation list was.

All six tables are tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa

revision = '025_requisitions_and_sourcing'
down_revision = '024_delegations_rls'
branch_labels = None
depends_on = None

UTC_NOW = sa.text("timezone('utc', now())")

REQUISITIONS = 'purchase_requisitions'
REQ_LINES = 'purchase_requisition_lines'
RFQS = 'rfqs'
RFQ_VENDORS = 'rfq_vendors'
QUOTES = 'quotes'
QUOTE_LINES = 'quote_lines'

ALL_TABLES = (REQUISITIONS, REQ_LINES, RFQS, RFQ_VENDORS, QUOTES, QUOTE_LINES)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
        USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)
    op.execute(f"""
        CREATE POLICY {table}_tenant_insert ON {table}
        FOR INSERT
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)


def upgrade() -> None:
    op.create_table(
        REQUISITIONS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('requisition_number', sa.String(100), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        # Required, not optional: an approval granted against an empty reason is
        # the record an auditor asks about first, and by then it is unrecoverable.
        sa.Column('justification', sa.String(), nullable=False),
        sa.Column('budget_code', sa.String(100), nullable=True),
        sa.Column('department', sa.String(100), nullable=True),
        sa.Column('requested_date', sa.Date(), nullable=False),
        sa.Column('needed_by', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=True),
        # The figure the approval was given against; an order raised later may
        # not exceed it without the requisition being re-approved.
        sa.Column('estimated_amount', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('current_state', sa.String(50), nullable=True),
        sa.Column('state_entered_at', sa.DateTime(), server_default=UTC_NOW, nullable=True),
        sa.Column('approved_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('rejection_reason', sa.String(), nullable=True),
        sa.Column('created_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
    )
    op.create_index(f'ix_{REQUISITIONS}_tenant_id', REQUISITIONS, ['tenant_id'])
    op.create_index(f'ix_{REQUISITIONS}_number', REQUISITIONS, ['requisition_number'])
    op.create_index(f'ix_{REQUISITIONS}_correlation_id', REQUISITIONS, ['correlation_id'])

    op.create_table(
        REQ_LINES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('requisition_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('product_code', sa.String(100), nullable=True),
        sa.Column('quantity', sa.Numeric(15, 3), nullable=False),
        sa.Column('estimated_unit_price', sa.Numeric(15, 2), nullable=False),
        sa.Column('estimated_amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requisition_id'], [f'{REQUISITIONS}.id'], ondelete='CASCADE'),
    )
    op.create_index(f'ix_{REQ_LINES}_tenant_id', REQ_LINES, ['tenant_id'])
    op.create_index(f'ix_{REQ_LINES}_requisition_id', REQ_LINES, ['requisition_id'])

    op.create_table(
        RFQS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('rfq_number', sa.String(100), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        # Required: an RFQ with no requisition behind it is a buyer approaching
        # the market on their own authority.
        sa.Column('requisition_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('issued_date', sa.Date(), nullable=True),
        sa.Column('closes_at', sa.DateTime(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=True),
        sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('current_state', sa.String(50), nullable=True),
        sa.Column('state_entered_at', sa.DateTime(), server_default=UTC_NOW, nullable=True),
        sa.Column('awarded_quote_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('awarded_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('awarded_at', sa.DateTime(), nullable=True),
        sa.Column('award_justification', sa.String(), nullable=True),
        sa.Column('cancellation_reason', sa.String(), nullable=True),
        sa.Column('created_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requisition_id'], [f'{REQUISITIONS}.id']),
        sa.ForeignKeyConstraint(['awarded_by'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
    )
    op.create_index(f'ix_{RFQS}_tenant_id', RFQS, ['tenant_id'])
    op.create_index(f'ix_{RFQS}_number', RFQS, ['rfq_number'])
    op.create_index(f'ix_{RFQS}_requisition_id', RFQS, ['requisition_id'])
    op.create_index(f'ix_{RFQS}_correlation_id', RFQS, ['correlation_id'])

    op.create_table(
        RFQ_VENDORS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('rfq_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_name', sa.String(255), nullable=False),
        sa.Column('invited_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['rfq_id'], [f'{RFQS}.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id']),
        # One invitation each; inviting twice would inflate the apparent field.
        sa.UniqueConstraint('rfq_id', 'vendor_id', name='uq_rfq_vendors_rfq_vendor'),
    )
    op.create_index(f'ix_{RFQ_VENDORS}_tenant_id', RFQ_VENDORS, ['tenant_id'])
    op.create_index(f'ix_{RFQ_VENDORS}_rfq_id', RFQ_VENDORS, ['rfq_id'])
    op.create_index(f'ix_{RFQ_VENDORS}_vendor_id', RFQ_VENDORS, ['vendor_id'])

    op.create_table(
        QUOTES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('rfq_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_name', sa.String(255), nullable=False),
        sa.Column('quote_reference', sa.String(100), nullable=True),
        sa.Column('quote_date', sa.Date(), nullable=True),
        sa.Column('valid_until', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=True),
        sa.Column('total_amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('lead_time_days', sa.Integer(), nullable=True),
        sa.Column('payment_terms', sa.String(255), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('is_compliant', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('non_compliance_reason', sa.String(), nullable=True),
        sa.Column('current_state', sa.String(50), nullable=True),
        sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        # Vendors do not log in, so the record says who typed it.
        sa.Column('captured_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['rfq_id'], [f'{RFQS}.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id']),
        sa.ForeignKeyConstraint(['captured_by'], ['users.id']),
        # One quote per vendor per tender. A revision withdraws the original
        # rather than silently replacing it, so both stay visible.
        sa.UniqueConstraint('rfq_id', 'vendor_id', name='uq_quotes_rfq_vendor'),
    )
    op.create_index(f'ix_{QUOTES}_tenant_id', QUOTES, ['tenant_id'])
    op.create_index(f'ix_{QUOTES}_rfq_id', QUOTES, ['rfq_id'])
    op.create_index(f'ix_{QUOTES}_vendor_id', QUOTES, ['vendor_id'])
    op.create_index(f'ix_{QUOTES}_correlation_id', QUOTES, ['correlation_id'])

    op.create_table(
        QUOTE_LINES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('quote_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('product_code', sa.String(100), nullable=True),
        sa.Column('quantity', sa.Numeric(15, 3), nullable=False),
        sa.Column('unit_price', sa.Numeric(15, 2), nullable=False),
        sa.Column('amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['quote_id'], [f'{QUOTES}.id'], ondelete='CASCADE'),
    )
    op.create_index(f'ix_{QUOTE_LINES}_tenant_id', QUOTE_LINES, ['tenant_id'])
    op.create_index(f'ix_{QUOTE_LINES}_quote_id', QUOTE_LINES, ['quote_id'])

    for table in ALL_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(ALL_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table(QUOTE_LINES)
    op.drop_table(QUOTES)
    op.drop_table(RFQ_VENDORS)
    op.drop_table(RFQS)
    op.drop_table(REQ_LINES)
    op.drop_table(REQUISITIONS)
