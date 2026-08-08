"""Goods receipts — the delivery side of procurement.

A receipt records what actually arrived against a purchase order. It has no
approval workflow of its own: it is a statement of fact, not a decision, so it
is audited and correlated but never routed. Correcting one means recording a
further receipt (a negative quantity for a return), which keeps the history
append-only rather than editing away what was once claimed to have arrived.

Receiving is what turns a two-way check into a three-way one. Without it the
system can only ask "does this invoice match what we ordered"; with it, it can
ask the question that actually stops fraud and error — "did this arrive at
all".
"""
from sqlalchemy import (
    Column, String, Date, Numeric, ForeignKey, Integer,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class GoodsReceipt(BaseModel):
    __tablename__ = "goods_receipts"

    #: Identifies this module in the audit trail and correlation chain. There
    #: is no WORKFLOW_TYPE because a receipt has no state machine.
    OBJECT_TYPE = "goods_receipt"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purchase_order_id = Column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    grn_number = Column(String(100), nullable=False, index=True)
    received_date = Column(Date, nullable=False)
    delivery_note = Column(String(255), nullable=True)
    notes = Column(String, nullable=True)

    #: Inherited from the purchase order, so the receipt lands in the same
    #: transaction story as the order and the invoice that settles it.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    received_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    tenant = relationship("Tenant", backref="goods_receipts")
    purchase_order = relationship("PurchaseOrder", backref="receipts")
    lines = relationship(
        "GoodsReceiptLine",
        back_populates="receipt",
        cascade="all, delete-orphan",
        order_by="GoodsReceiptLine.line_number",
    )


class GoodsReceiptLine(BaseModel):
    """What arrived against one purchase order line."""
    __tablename__ = "goods_receipt_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    goods_receipt_id = Column(
        UUID(as_uuid=True), ForeignKey("goods_receipts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purchase_order_line_id = Column(
        UUID(as_uuid=True), ForeignKey("purchase_order_lines.id"),
        nullable=False, index=True,
    )

    line_number = Column(Integer, nullable=False)
    #: Negative on a return, so a correction is recorded rather than erased.
    quantity_received = Column(Numeric(15, 3), nullable=False)

    receipt = relationship("GoodsReceipt", back_populates="lines")
    purchase_order_line = relationship("PurchaseOrderLine", backref="receipt_lines")
