"""Inventory: what is on hand, and every reason it changed.

Build Book, Variant D1 "Inventory and Receiving": receiving, GRN, quality
checks, putaway and stock updates; inventory adjustments with thresholds and
evidence; returns with reason codes and vendor accountability.

Receiving already existed, but only as an input to three-way matching — a
statement that something arrived, with nowhere for it to arrive *to*. This is
the other half.

The decision that shapes everything here is that **stock is a ledger, not a
number**. `StockMovement` is append-only: every change is a row carrying its
quantity, its reason and what caused it, and the balance is the sum. The
tempting alternative — a quantity column that gets updated — cannot answer the
question an auditor actually asks, which is not "what is the stock" but "why is
it that, and who changed it". It is also the same principle the audit trail and
the soft-delete guard already enforce: in this system nothing is edited away.

`StockBalance` is a maintained aggregate over that ledger, kept because
"what is on hand right now" is asked on every receipt, every adjustment and
every stockout check, and summing a ledger that grows forever to answer it
gets slower every day. It is derived data and is rebuildable — the ledger is
the truth, and a test proves the two agree.
"""
from sqlalchemy import (
    Column, String, Numeric, Boolean, DateTime, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, SoftDeleteMixin

# --- movement kinds ---------------------------------------------------------
#: Why stock moved. Stored rather than inferred from what is linked, because a
#: movement has to explain itself in a report years later when the record that
#: caused it may have been withdrawn.
MOVE_RECEIPT = "receipt"
MOVE_ADJUSTMENT = "adjustment"
MOVE_RETURN = "return"
MOVE_TRANSFER = "transfer"
MOVE_ISSUE = "issue"

MOVEMENT_TYPES = (
    MOVE_RECEIPT, MOVE_ADJUSTMENT, MOVE_RETURN, MOVE_TRANSFER, MOVE_ISSUE,
)

# --- reason codes -----------------------------------------------------------
#: Build Book: "returns management with reason codes and vendor
#: accountability", and "damage and shortage evidence requirements". Reason
#: codes are a fixed vocabulary rather than free text precisely so they can be
#: counted — "which vendor damages the most goods" is unanswerable if every
#: receiver types their own word for damaged.
REASON_DAMAGED = "damaged"
REASON_SHORTAGE = "shortage"
REASON_OVERAGE = "overage"
REASON_WRONG_ITEM = "wrong_item"
REASON_QUALITY_FAILURE = "quality_failure"
REASON_EXPIRED = "expired"
REASON_COUNT_CORRECTION = "count_correction"
REASON_THEFT_OR_LOSS = "theft_or_loss"

REASON_CODES = (
    REASON_DAMAGED, REASON_SHORTAGE, REASON_OVERAGE, REASON_WRONG_ITEM,
    REASON_QUALITY_FAILURE, REASON_EXPIRED, REASON_COUNT_CORRECTION,
    REASON_THEFT_OR_LOSS,
)

#: Reasons that are the vendor's responsibility. Separated here rather than
#: judged at the report, so "supplier accountability" means one agreed list
#: instead of whatever each dashboard happens to filter on.
VENDOR_ATTRIBUTABLE_REASONS = (
    REASON_DAMAGED, REASON_SHORTAGE, REASON_WRONG_ITEM, REASON_QUALITY_FAILURE,
)


class Item(BaseModel, SoftDeleteMixin):
    """The item master.

    Soft-deletable, not hard: an item that is discontinued still appears in
    last year's receipts and adjustments, and deleting the row would leave the
    audit trail pointing at nothing.
    """
    __tablename__ = "items"

    OBJECT_TYPE = "item"
    REFERENCE_FIELD = "sku"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    sku = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(String, nullable=True)
    category = Column(String(100), nullable=True, index=True)

    #: Unit of measure. Free text on purpose: "each", "kg", "carton of 12" are
    #: all things clients say, and an enum here would be wrong within a week.
    uom = Column(String(32), nullable=False, default="each")

    #: A non-stocked item is bought but never held — services, one-off spend.
    #: It can appear on an order and a receipt and never touches a balance,
    #: which is why receiving alone could never imply inventory.
    is_stocked = Column(Boolean, nullable=False, default=True)

    #: Below this, the item is at stockout risk. Nullable because most items
    #: never get one set, and a default of zero would quietly mean "never at
    #: risk" while looking configured.
    reorder_point = Column(Numeric(15, 3), nullable=True)

    #: For valuing stock on hand. Standard cost rather than moving average:
    #: the moving average needs a costing engine and a posting model, which is
    #: a finance module, not this one.
    standard_cost = Column(Numeric(15, 2), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)

    tenant = relationship("Tenant", backref="items")

    __table_args__ = (
        UniqueConstraint("tenant_id", "sku", name="uq_items_tenant_sku"),
    )


class StockLocation(BaseModel, SoftDeleteMixin):
    """Where stock sits: a warehouse, a store, a bin.

    Separate from `OrgUnit` even though the Build Book lists "location" as an
    org unit type, because the two answer different questions. An org unit is
    who may act on a record; a stock location is where a physical thing is.
    One warehouse can serve several business units, and scoping stock by the
    approver's department would be nonsense. `org_unit_id` links them where a
    client does want that alignment.
    """
    __tablename__ = "stock_locations"

    OBJECT_TYPE = "stock_location"
    REFERENCE_FIELD = "code"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)

    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True,
    )

    #: Goods that arrived but have not been put away or inspected sit here.
    #: Build Book: "putaway". Without a staging location, receiving would have
    #: to claim goods are shelved the instant the lorry leaves.
    is_receiving_bay = Column(Boolean, nullable=False, default=False)

    #: Failed quality checks go here rather than back into stock, so that
    #: rejected goods cannot be picked while somebody decides what to do.
    is_quarantine = Column(Boolean, nullable=False, default=False)

    is_active = Column(Boolean, nullable=False, default=True)

    org_unit = relationship("OrgUnit", backref="stock_locations")

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_stock_locations_tenant_code"),
    )


class StockMovement(BaseModel):
    """One change in stock. Append-only.

    Deliberately *not* soft-deletable and never updated. A movement is a
    historical fact like an audit entry: correcting one means posting an
    opposing movement, which is why `quantity` is signed. Editing one would
    make the balance disagree with its own history with nothing to show what
    happened — the exact failure the ledger exists to prevent.
    """
    __tablename__ = "stock_movements"

    OBJECT_TYPE = "stock_movement"
    #: A movement has no number of its own — it is identified by what it did.
    #: Composed from its own columns so nothing lazy-loads while a timeline is
    #: being rendered; without this the audit trail shows a raw UUID.
    REFERENCE_FIELD = "reference"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    item_id = Column(
        UUID(as_uuid=True), ForeignKey("items.id"), nullable=False, index=True,
    )
    location_id = Column(
        UUID(as_uuid=True), ForeignKey("stock_locations.id"),
        nullable=False, index=True,
    )

    #: Signed: positive adds, negative removes. One column rather than separate
    #: in/out columns so the balance is a plain SUM and cannot be got wrong by
    #: adding the pair up in the wrong order.
    quantity = Column(Numeric(15, 3), nullable=False)

    movement_type = Column(String(20), nullable=False, index=True)
    reason_code = Column(String(40), nullable=True, index=True)

    #: What caused it — a goods receipt, an adjustment, a return. Kept as a
    #: type/id pair rather than five nullable foreign keys, matching how the
    #: audit trail already refers to records.
    source_type = Column(String(50), nullable=True)
    source_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    note = Column(String(500), nullable=True)

    created_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True,
    )
    #: Ties a movement into the transaction chain it belongs to, so an evidence
    #: pack for an invoice can show the goods that arrived against it.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    item = relationship("Item", backref="movements")
    location = relationship("StockLocation", backref="movements")

    @property
    def reference(self) -> str:
        """"receipt +7" — what a person would say about this row."""
        quantity = self.quantity if self.quantity is not None else 0
        return f"{self.movement_type} {quantity:+}"

    __table_args__ = (
        # The two questions asked constantly: this item's balance at this
        # location, and this item's recent history.
        Index("ix_stock_movements_item_location", "item_id", "location_id"),
        Index("ix_stock_movements_tenant_created", "tenant_id", "created_at"),
    )


class StockBalance(BaseModel):
    """On-hand quantity per item per location.

    Derived from `StockMovement` and maintained as movements are posted.
    Rebuildable from the ledger at any time, which is what makes keeping it
    safe — and a test asserts the two agree, because a cached total that can
    drift from its source without anybody noticing is worse than no cache.
    """
    __tablename__ = "stock_balances"

    OBJECT_TYPE = "stock_balance"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    item_id = Column(
        UUID(as_uuid=True), ForeignKey("items.id"), nullable=False, index=True,
    )
    location_id = Column(
        UUID(as_uuid=True), ForeignKey("stock_locations.id"),
        nullable=False, index=True,
    )

    quantity = Column(Numeric(15, 3), nullable=False, default=0)

    #: When a movement last touched this balance. Distinct from `updated_at`,
    #: which changes for any write at all.
    last_movement_at = Column(DateTime, nullable=True)

    item = relationship("Item", backref="balances")
    location = relationship("StockLocation", backref="balances")

    __table_args__ = (
        # One row per item per location, enforced in the database rather than
        # in the service: two concurrent receipts creating two balance rows for
        # the same pair would each hold half the stock, and every later read
        # would silently pick one.
        UniqueConstraint(
            "item_id", "location_id", name="uq_stock_balances_item_location"
        ),
    )
