"""The controlled ways stock changes: adjustments, quality checks, returns.

Build Book, Variant D1 controls:

  * "Inventory adjustments approval with thresholds and evidence."
  * "Adjustment thresholds with dual approval above limit."
  * "SoD separation between receiver and approver."
  * "Damage and shortage evidence requirements with photos and QC notes."
  * "Returns management with reason codes and vendor accountability."

An adjustment is the only way stock changes without something physically
arriving or leaving, which makes it the fraud surface of this module: writing
off stock is how a theft is covered up. So it is the one inventory record with
a full approval workflow, a value threshold, an evidence requirement and an SoD
rule — the same shape the invoice already has, for the same reason.

Quality checks and returns hang off receiving. A check is a decision about
goods that arrived; a return is what happens to the ones that failed, and it is
where vendor accountability is recorded.
"""
from sqlalchemy import (
    Column, String, Numeric, DateTime, ForeignKey, Integer, Boolean,
    UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, SoftDeleteMixin

# --- adjustment states ------------------------------------------------------
ADJ_DRAFT = "draft"
ADJ_PENDING_APPROVAL = "pending_approval"
ADJ_APPROVED = "approved"
ADJ_POSTED = "posted"
ADJ_REJECTED = "rejected"
ADJ_CANCELLED = "cancelled"

# --- quality check outcomes -------------------------------------------------
QC_PENDING = "pending"
QC_PASSED = "passed"
QC_FAILED = "failed"
QC_PARTIAL = "partial"

QC_OUTCOMES = (QC_PENDING, QC_PASSED, QC_FAILED, QC_PARTIAL)

# --- return states ----------------------------------------------------------
RET_DRAFT = "draft"
RET_PENDING_APPROVAL = "pending_approval"
RET_APPROVED = "approved"
RET_DISPATCHED = "dispatched"
RET_CREDITED = "credited"
RET_REJECTED = "rejected"
RET_CANCELLED = "cancelled"


class InventoryAdjustment(BaseModel, SoftDeleteMixin):
    """A change to stock with no delivery behind it.

    The governed record of this module. `posted` is separate from `approved`
    because approving is a decision and posting is what actually moves the
    ledger; keeping them apart means an approval that fails to post is visible
    as an approved-but-unposted row rather than as stock that silently never
    changed.
    """
    __tablename__ = "inventory_adjustments"

    OBJECT_TYPE = "inventory_adjustment"
    REFERENCE_FIELD = "adjustment_number"
    #: Puts this record in front of the SLA and escalation runner, which scans
    #: whatever declares it. Without this the states below have deadlines that
    #: nothing ever reads — which is not a deadline.
    WORKFLOW_TYPE = "inventory_adjustment"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    adjustment_number = Column(String(64), nullable=False, index=True)
    location_id = Column(
        UUID(as_uuid=True), ForeignKey("stock_locations.id"),
        nullable=False, index=True,
    )

    reason_code = Column(String(40), nullable=False, index=True)
    reason_note = Column(String, nullable=True)

    current_state = Column(
        String(30), nullable=False, default=ADJ_DRAFT, index=True,
    )
    #: When this record entered its current state. The SLA is computed from
    #: here at read time, so changing an SLA setting re-prices every open timer
    #: rather than only new ones.
    state_entered_at = Column(DateTime, nullable=True)

    #: Absolute value of the adjustment at standard cost, cached at submission.
    #: The threshold that decides who must approve is a money question, and
    #: recomputing it at approval time would let the required approver change
    #: after the fact if a cost were edited in between.
    total_value = Column(Numeric(15, 2), nullable=False, default=0)

    #: Whether this one crossed the dual-approval limit. Stored rather than
    #: recomputed so the audit trail shows the rule as it stood that day, the
    #: same reason policy evaluations are snapshotted.
    requires_dual_approval = Column(Boolean, nullable=False, default=False)

    created_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    submitted_at = Column(DateTime, nullable=True)

    #: First approver.
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    #: Second approver, above the limit. A separate column rather than a count,
    #: because "who were the two people" is the question an auditor asks, and a
    #: counter cannot answer it.
    second_approved_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True,
    )
    second_approved_at = Column(DateTime, nullable=True)

    posted_at = Column(DateTime, nullable=True)
    rejected_reason = Column(String, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True,
    )

    location = relationship("StockLocation", backref="adjustments")
    lines = relationship(
        "InventoryAdjustmentLine", back_populates="adjustment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "adjustment_number", name="uq_adjustments_tenant_number"
        ),
    )


class InventoryAdjustmentLine(BaseModel):
    __tablename__ = "inventory_adjustment_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    adjustment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("inventory_adjustments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    item_id = Column(
        UUID(as_uuid=True), ForeignKey("items.id"), nullable=False, index=True,
    )

    line_number = Column(Integer, nullable=False)

    #: Signed, like a movement: negative writes stock off, positive writes it
    #: on. Writing off is the direction that hides a theft, and the reports
    #: separate the two for that reason.
    quantity_change = Column(Numeric(15, 3), nullable=False)

    #: What the system thought was there when the line was raised. Kept so a
    #: count correction can be read back as "expected 40, found 37" rather than
    #: as a bare -3 that means nothing later.
    quantity_before = Column(Numeric(15, 3), nullable=True)

    unit_cost = Column(Numeric(15, 2), nullable=True)
    note = Column(String(500), nullable=True)

    adjustment = relationship("InventoryAdjustment", back_populates="lines")
    item = relationship("Item")


class QualityCheck(BaseModel):
    """An inspection of goods that arrived.

    Attached to a goods receipt line, because "did this delivery pass" is a
    question about a specific delivery of a specific item. Failing a check does
    not delete the receipt — what arrived still arrived — it moves the goods to
    quarantine and opens the question of a return.
    """
    __tablename__ = "quality_checks"

    OBJECT_TYPE = "quality_check"
    #: Like a stock movement, a check has no number — it is identified by its
    #: outcome. Composed from its own columns, so rendering a timeline does not
    #: lazy-load the receipt behind it.
    REFERENCE_FIELD = "reference"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    goods_receipt_line_id = Column(
        UUID(as_uuid=True), ForeignKey("goods_receipt_lines.id"),
        nullable=False, index=True,
    )

    outcome = Column(String(20), nullable=False, default=QC_PENDING, index=True)

    quantity_accepted = Column(Numeric(15, 3), nullable=False, default=0)
    quantity_rejected = Column(Numeric(15, 3), nullable=False, default=0)

    reason_code = Column(String(40), nullable=True, index=True)
    notes = Column(String, nullable=True)

    inspected_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    inspected_at = Column(DateTime, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    goods_receipt_line = relationship("GoodsReceiptLine", backref="quality_checks")

    @property
    def reference(self) -> str:
        return f"quality check {self.outcome}"


class VendorReturn(BaseModel, SoftDeleteMixin):
    """Goods going back to the vendor, and who is answerable for them.

    Named `VendorReturn` rather than `Return` because `return` is a Python
    keyword and a model called `Return` makes every import read like a bug.
    """
    __tablename__ = "vendor_returns"

    OBJECT_TYPE = "vendor_return"
    REFERENCE_FIELD = "return_number"
    WORKFLOW_TYPE = "vendor_return"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    return_number = Column(String(64), nullable=False, index=True)

    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True,
    )
    purchase_order_id = Column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id"),
        nullable=True, index=True,
    )
    location_id = Column(
        UUID(as_uuid=True), ForeignKey("stock_locations.id"),
        nullable=False, index=True,
    )

    reason_code = Column(String(40), nullable=False, index=True)
    reason_note = Column(String, nullable=True)

    #: Whether this return is the vendor's fault. Derived from the reason code
    #: at creation and then stored, so a later change to what counts as
    #: vendor-attributable does not rewrite history — a supplier scorecard that
    #: changes retrospectively is not evidence.
    vendor_attributable = Column(Boolean, nullable=False, default=False)

    current_state = Column(
        String(30), nullable=False, default=RET_DRAFT, index=True,
    )
    state_entered_at = Column(DateTime, nullable=True)

    total_value = Column(Numeric(15, 2), nullable=False, default=0)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    dispatched_at = Column(DateTime, nullable=True)

    #: The credit note the vendor eventually issues. Nullable and often unset
    #: for a long time, which is exactly what the ageing report is for.
    credit_note_reference = Column(String(100), nullable=True)
    credited_at = Column(DateTime, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True,
    )

    vendor = relationship("Vendor", backref="returns")
    location = relationship("StockLocation", backref="returns")
    lines = relationship(
        "VendorReturnLine", back_populates="vendor_return",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "return_number", name="uq_returns_tenant_number"
        ),
        Index("ix_vendor_returns_vendor_state", "vendor_id", "current_state"),
    )


class VendorReturnLine(BaseModel):
    __tablename__ = "vendor_return_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    return_id = Column(
        UUID(as_uuid=True), ForeignKey("vendor_returns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    item_id = Column(
        UUID(as_uuid=True), ForeignKey("items.id"), nullable=False, index=True,
    )
    goods_receipt_line_id = Column(
        UUID(as_uuid=True), ForeignKey("goods_receipt_lines.id"),
        nullable=True, index=True,
    )

    line_number = Column(Integer, nullable=False)
    quantity = Column(Numeric(15, 3), nullable=False)
    unit_cost = Column(Numeric(15, 2), nullable=True)
    note = Column(String(500), nullable=True)

    vendor_return = relationship("VendorReturn", back_populates="lines")
    item = relationship("Item")
