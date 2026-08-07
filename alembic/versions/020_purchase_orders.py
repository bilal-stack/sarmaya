"""add purchase orders and their lines

Revision ID: 020_purchase_orders
Revises: 019_utc_timestamp_defaults
Create Date: 2026-08-06 00:00:00.000000

The commitment side of procurement: a purchase order and its lines. Quantities
live on the lines because three-way matching is per line — a delivery can be
partial, and an invoice for more than was received has to be detectable line by
line.

Both tables are tenant-scoped with the usual ENABLE + FORCE RLS isolation, and
carry a correlation_id so a PO starts the transaction story that its receipts
and invoice later join.

Replaces a model stub that was never registered, so there is no existing table
to migrate from.
"""
from alembic import op
import sqlalchemy as sa

revision = '020_purchase_orders'
down_revision = '019_utc_timestamp_defaults'
branch_labels = None
depends_on = None

UTC_NOW = sa.text("timezone('utc', now())")
ORDERS = 'purchase_orders'
LINES = 'purchase_order_lines'


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # FORCE so the table owner is bound by the policy too; without it, DDL-owning
    # roles silently bypass tenant isolation.
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
        ORDERS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('po_number', sa.String(100), nullable=False),
        sa.Column('vendor_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('vendor_name', sa.String(255), nullable=False),
        sa.Column('order_date', sa.Date(), nullable=False),
        sa.Column('expected_date', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=True),
        sa.Column('subtotal_amount', sa.Numeric(15, 2), nullable=True),
        sa.Column('tax_amount', sa.Numeric(15, 2), nullable=True),
        sa.Column('total_amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
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
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
    )
    op.create_index(f'ix_{ORDERS}_tenant_id', ORDERS, ['tenant_id'])
    op.create_index(f'ix_{ORDERS}_po_number', ORDERS, ['po_number'])
    op.create_index(f'ix_{ORDERS}_correlation_id', ORDERS, ['correlation_id'])

    op.create_table(
        LINES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('purchase_order_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('product_code', sa.String(100), nullable=True),
        sa.Column('quantity', sa.Numeric(15, 3), nullable=False),
        sa.Column('unit_price', sa.Numeric(15, 2), nullable=False),
        sa.Column('amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('received_quantity', sa.Numeric(15, 3), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['purchase_order_id'], [f'{ORDERS}.id'], ondelete='CASCADE'),
    )
    op.create_index(f'ix_{LINES}_tenant_id', LINES, ['tenant_id'])
    op.create_index(f'ix_{LINES}_purchase_order_id', LINES, ['purchase_order_id'])

    for table in (ORDERS, LINES):
        _enable_rls(table)


def downgrade() -> None:
    for table in (LINES, ORDERS):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table(LINES)
    op.drop_table(ORDERS)
