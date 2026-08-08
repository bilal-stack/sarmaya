"""Purchase orders — the commitment side of procurement.

Replaces a stub that was never usable: it was not imported into the model
registry so no table was ever created, its vendor_id was an Integer against a
UUID primary key, and it had no tenant_id, which would have placed it outside
both RLS and the application-level tenant scoping. A module built on it would
have had no tenant boundary at all.

The shape deliberately mirrors Invoice — tenant_id, correlation_id,
current_state, state_entered_at — so the governance layer applies without
special cases: the same approval matrix routes it, the same guards gate its
transitions, the same hash-chained audit records it, and it joins the same
correlation chain as the invoice that eventually settles it.
"""
from sqlalchemy import (
    Column, String, Date, Numeric, ForeignKey, DateTime, Integer,
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, UTC_NOW
from app.core.enums import PurchaseOrderState, Currency


class PurchaseOrder(BaseModel):
    __tablename__ = "purchase_orders"

    #: Which configured state machine governs this record.
    WORKFLOW_TYPE = "purchase_order"

    #: How this module names itself in the audit trail and correlation chain.
    OBJECT_TYPE = "purchase_order"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    po_number = Column(String(100), nullable=False, index=True)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255), nullable=False)

    order_date = Column(Date, nullable=False)
    expected_date = Column(Date, nullable=True)

    currency = Column(SQLEnum(Currency), default=Currency.PKR)
    subtotal_amount = Column(Numeric(15, 2), nullable=True)
    tax_amount = Column(Numeric(15, 2), nullable=True)
    total_amount = Column(Numeric(15, 2), nullable=False)

    description = Column(String, nullable=True)

    # Chain identity: a PO starts the transaction story that its receipts and
    # invoice later join (Build Book: universal correlation IDs).
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    current_state = Column(SQLEnum(PurchaseOrderState), default=PurchaseOrderState.DRAFT)
    # SLA timer start, UTC for the same reason as Invoice.state_entered_at.
    state_entered_at = Column(DateTime, server_default=UTC_NOW, nullable=True)

    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    tenant = relationship("Tenant", backref="purchase_orders")
    vendor = relationship("Vendor", backref="purchase_orders")
    lines = relationship(
        "PurchaseOrderLine",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        order_by="PurchaseOrderLine.line_number",
    )


class PurchaseOrderLine(BaseModel):
    """One ordered item.

    Quantities live here rather than on the order because three-way matching
    is per line: a delivery can be partial, and an invoice for more than was
    received has to be detectable line by line.
    """
    __tablename__ = "purchase_order_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purchase_order_id = Column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    line_number = Column(Integer, nullable=False)
    description = Column(String, nullable=False)
    product_code = Column(String(100), nullable=True)

    quantity = Column(Numeric(15, 3), nullable=False)
    unit_price = Column(Numeric(15, 2), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)

    #: Running total of what has actually arrived, raised by each goods
    #: receipt. Kept on the line so "ordered vs received" is answerable
    #: without replaying every receipt.
    received_quantity = Column(Numeric(15, 3), nullable=False, default=0)

    purchase_order = relationship("PurchaseOrder", back_populates="lines")
