"""connect receiving to the stock ledger

Revision ID: 039_link_receiving_to_stock
Revises: 038_inventory
Create Date: 2026-08-20 00:00:00.000000

Receiving existed to serve three-way matching: it recorded that a quantity
arrived against an order line so the match could ask "did this turn up". It
never said *what* arrived in item-master terms, or *where* it went, because
until migration 038 there was no item master and nowhere for anything to go.

Two nullable columns close that gap:

  * `purchase_order_lines.item_id` — the line's item, when it has one. Nullable
    on purpose and permanently: services and one-off spend are ordered and
    received without ever being held, and forcing every line to name a stocked
    item would make the order form lie about what is being bought.
  * `goods_receipts.location_id` — where the delivery landed, normally the
    receiving bay. Nullable because every receipt recorded before this
    migration happened somewhere nobody wrote down, and inventing a location
    for them would be fabricating history.

Neither is backfilled. A receipt only moves stock when its line names a stocked
item and the receipt names a location, so existing data stays exactly as it is
and nothing retroactively appears on a shelf.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '039_link_receiving_to_stock'
down_revision = '038_inventory'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'purchase_order_lines',
        sa.Column('item_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('items.id'), nullable=True),
    )
    op.create_index(
        'ix_purchase_order_lines_item_id', 'purchase_order_lines', ['item_id'],
    )

    op.add_column(
        'goods_receipts',
        sa.Column('location_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('stock_locations.id'), nullable=True),
    )
    op.create_index(
        'ix_goods_receipts_location_id', 'goods_receipts', ['location_id'],
    )


def downgrade():
    op.drop_index('ix_goods_receipts_location_id', table_name='goods_receipts')
    op.drop_column('goods_receipts', 'location_id')
    op.drop_index('ix_purchase_order_lines_item_id', table_name='purchase_order_lines')
    op.drop_column('purchase_order_lines', 'item_id')
