"""inventory: the other half of Variant D

Revision ID: 038_inventory
Revises: 037_job_runs
Create Date: 2026-08-20 00:00:00.000000

Build Book, Variant D1 "Inventory and Receiving". Receiving already existed —
`goods_receipts` records what arrived against an order so three-way matching
can ask "did this turn up". What was missing was anywhere for it to arrive
*to*: no item master, no locations, no balances, and no governed way to change
stock without a delivery behind it.

The shape worth noting is that stock is a **ledger**, not a number.
`stock_movements` is append-only and signed; `stock_balances` is a maintained
aggregate over it, carrying a unique constraint on (item, location) so two
concurrent receipts cannot create two rows that each hold half the stock. The
balance is rebuildable from the ledger, which is what makes keeping it safe.

`inventory_adjustments` is the governed record here — the only way stock moves
with nothing physical behind it, and therefore how a theft gets covered up. It
gets the full treatment: workflow states, a value threshold, dual approval
above the limit, and both approvers recorded by name rather than counted.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '038_inventory'
down_revision = '037_job_runs'
branch_labels = None
depends_on = None

#: Every new table is tenant-scoped and gets the same RLS treatment as the
#: rest of the schema. Listed once so none can be missed — a table added to
#: this migration without a policy would be readable across tenants, and
#: nothing in the application would notice.
NEW_TABLES = (
    'items',
    'stock_locations',
    'stock_movements',
    'stock_balances',
    'inventory_adjustments',
    'inventory_adjustment_lines',
    'quality_checks',
    'vendor_returns',
    'vendor_return_lines',
)


def _timestamps():
    return (
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
    )


def _soft_delete():
    return (
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('deletion_reason', sa.String(500), nullable=True),
    )


def _tenant_fk():
    return sa.Column(
        'tenant_id', postgresql.UUID(as_uuid=True),
        sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
        index=True,
    )


def upgrade():
    op.create_table(
        'items',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('sku', sa.String(64), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('category', sa.String(100), nullable=True, index=True),
        sa.Column('uom', sa.String(32), nullable=False, server_default='each'),
        sa.Column('is_stocked', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('reorder_point', sa.Numeric(15, 3), nullable=True),
        sa.Column('standard_cost', sa.Numeric(15, 2), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'sku', name='uq_items_tenant_sku'),
    )

    op.create_table(
        'stock_locations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('code', sa.String(50), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id'), nullable=True, index=True),
        sa.Column('is_receiving_bay', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('is_quarantine', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'code', name='uq_stock_locations_tenant_code'),
    )

    op.create_table(
        'stock_movements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('item_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('items.id'), nullable=False, index=True),
        sa.Column('location_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('stock_locations.id'), nullable=False, index=True),
        sa.Column('quantity', sa.Numeric(15, 3), nullable=False),
        sa.Column('movement_type', sa.String(20), nullable=False, index=True),
        sa.Column('reason_code', sa.String(40), nullable=True, index=True),
        sa.Column('source_type', sa.String(50), nullable=True),
        sa.Column('source_id', postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_timestamps(),
    )
    op.create_index(
        'ix_stock_movements_item_location', 'stock_movements',
        ['item_id', 'location_id'],
    )
    op.create_index(
        'ix_stock_movements_tenant_created', 'stock_movements',
        ['tenant_id', 'created_at'],
    )

    op.create_table(
        'stock_balances',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('item_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('items.id'), nullable=False, index=True),
        sa.Column('location_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('stock_locations.id'), nullable=False, index=True),
        sa.Column('quantity', sa.Numeric(15, 3), nullable=False, server_default='0'),
        sa.Column('last_movement_at', sa.DateTime(), nullable=True),
        *_timestamps(),
        # In the database, not the service: two concurrent receipts creating
        # two balance rows for one (item, location) would each hold half the
        # stock, and every later read would silently pick one of them.
        sa.UniqueConstraint('item_id', 'location_id',
                            name='uq_stock_balances_item_location'),
    )

    op.create_table(
        'inventory_adjustments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('adjustment_number', sa.String(64), nullable=False, index=True),
        sa.Column('location_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('stock_locations.id'), nullable=False, index=True),
        sa.Column('reason_code', sa.String(40), nullable=False, index=True),
        sa.Column('reason_note', sa.String(), nullable=True),
        sa.Column('current_state', sa.String(30), nullable=False,
                  server_default='draft', index=True),
        sa.Column('total_value', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('requires_dual_approval', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('second_approved_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('second_approved_at', sa.DateTime(), nullable=True),
        sa.Column('posted_at', sa.DateTime(), nullable=True),
        sa.Column('rejected_reason', sa.String(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id'), nullable=True, index=True),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'adjustment_number',
                            name='uq_adjustments_tenant_number'),
    )

    op.create_table(
        'inventory_adjustment_lines',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('adjustment_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('inventory_adjustments.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('item_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('items.id'), nullable=False, index=True),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('quantity_change', sa.Numeric(15, 3), nullable=False),
        sa.Column('quantity_before', sa.Numeric(15, 3), nullable=True),
        sa.Column('unit_cost', sa.Numeric(15, 2), nullable=True),
        sa.Column('note', sa.String(500), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        'quality_checks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('goods_receipt_line_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('goods_receipt_lines.id'), nullable=False, index=True),
        sa.Column('outcome', sa.String(20), nullable=False,
                  server_default='pending', index=True),
        sa.Column('quantity_accepted', sa.Numeric(15, 3), nullable=False,
                  server_default='0'),
        sa.Column('quantity_rejected', sa.Numeric(15, 3), nullable=False,
                  server_default='0'),
        sa.Column('reason_code', sa.String(40), nullable=True, index=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('inspected_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('inspected_at', sa.DateTime(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_timestamps(),
    )

    op.create_table(
        'vendor_returns',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('return_number', sa.String(64), nullable=False, index=True),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('vendors.id'), nullable=False, index=True),
        sa.Column('purchase_order_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('purchase_orders.id'), nullable=True, index=True),
        sa.Column('location_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('stock_locations.id'), nullable=False, index=True),
        sa.Column('reason_code', sa.String(40), nullable=False, index=True),
        sa.Column('reason_note', sa.String(), nullable=True),
        sa.Column('vendor_attributable', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('current_state', sa.String(30), nullable=False,
                  server_default='draft', index=True),
        sa.Column('total_value', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('dispatched_at', sa.DateTime(), nullable=True),
        sa.Column('credit_note_reference', sa.String(100), nullable=True),
        sa.Column('credited_at', sa.DateTime(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id'), nullable=True, index=True),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'return_number',
                            name='uq_returns_tenant_number'),
    )
    op.create_index(
        'ix_vendor_returns_vendor_state', 'vendor_returns',
        ['vendor_id', 'current_state'],
    )

    op.create_table(
        'vendor_return_lines',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('return_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('vendor_returns.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('item_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('items.id'), nullable=False, index=True),
        sa.Column('goods_receipt_line_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('goods_receipt_lines.id'), nullable=True, index=True),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Numeric(15, 3), nullable=False),
        sa.Column('unit_cost', sa.Numeric(15, 2), nullable=True),
        sa.Column('note', sa.String(500), nullable=True),
        *_timestamps(),
    )

    for table in NEW_TABLES:
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


def downgrade():
    for table in NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    # Reverse dependency order.
    op.drop_table('vendor_return_lines')
    op.drop_index('ix_vendor_returns_vendor_state', table_name='vendor_returns')
    op.drop_table('vendor_returns')
    op.drop_table('quality_checks')
    op.drop_table('inventory_adjustment_lines')
    op.drop_table('inventory_adjustments')
    op.drop_table('stock_balances')
    op.drop_index('ix_stock_movements_tenant_created', table_name='stock_movements')
    op.drop_index('ix_stock_movements_item_location', table_name='stock_movements')
    op.drop_table('stock_movements')
    op.drop_table('stock_locations')
    op.drop_table('items')
