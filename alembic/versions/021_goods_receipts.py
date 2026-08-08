"""add goods receipts and link invoices to purchase orders

Revision ID: 021_goods_receipts
Revises: 020_purchase_orders
Create Date: 2026-08-08 00:00:00.000000

Goods receipts record what actually arrived against a purchase order. They have
no workflow of their own — a receipt is a statement of fact, not a decision —
so they carry a correlation_id and are audited, but are never routed for
approval. A correction is a further receipt with a negative quantity, keeping
the history append-only rather than editing away what was once claimed to have
arrived.

Also adds the foreign key that invoices.purchase_order_id never had. The column
already existed but pointed at nothing enforceable, so an invoice could name a
purchase order that does not exist — and three-way matching is about to trust
that link.
"""
from alembic import op
import sqlalchemy as sa

revision = '021_goods_receipts'
down_revision = '020_purchase_orders'
branch_labels = None
depends_on = None

UTC_NOW = sa.text("timezone('utc', now())")
RECEIPTS = 'goods_receipts'
LINES = 'goods_receipt_lines'
INVOICE_PO_FK = 'fk_invoices_purchase_order_id'


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
        RECEIPTS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('purchase_order_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('grn_number', sa.String(100), nullable=False),
        sa.Column('received_date', sa.Date(), nullable=False),
        sa.Column('delivery_note', sa.String(255), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('received_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['received_by'], ['users.id']),
    )
    op.create_index(f'ix_{RECEIPTS}_tenant_id', RECEIPTS, ['tenant_id'])
    op.create_index(f'ix_{RECEIPTS}_purchase_order_id', RECEIPTS, ['purchase_order_id'])
    op.create_index(f'ix_{RECEIPTS}_grn_number', RECEIPTS, ['grn_number'])
    op.create_index(f'ix_{RECEIPTS}_correlation_id', RECEIPTS, ['correlation_id'])

    op.create_table(
        LINES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('goods_receipt_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('purchase_order_line_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('quantity_received', sa.Numeric(15, 3), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['goods_receipt_id'], [f'{RECEIPTS}.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['purchase_order_line_id'], ['purchase_order_lines.id']),
    )
    op.create_index(f'ix_{LINES}_tenant_id', LINES, ['tenant_id'])
    op.create_index(f'ix_{LINES}_goods_receipt_id', LINES, ['goods_receipt_id'])

    for table in (RECEIPTS, LINES):
        _enable_rls(table)

    # The link three-way matching is about to trust. Any row pointing at a
    # purchase order that does not exist is cleared first, since the column has
    # been unconstrained until now.
    op.execute("""
        UPDATE invoices SET purchase_order_id = NULL
        WHERE purchase_order_id IS NOT NULL
          AND purchase_order_id NOT IN (SELECT id FROM purchase_orders)
    """)
    op.create_foreign_key(
        INVOICE_PO_FK, 'invoices', 'purchase_orders',
        ['purchase_order_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint(INVOICE_PO_FK, 'invoices', type_='foreignkey')
    for table in (LINES, RECEIPTS):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table(LINES)
    op.drop_table(RECEIPTS)
