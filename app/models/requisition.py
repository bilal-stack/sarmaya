"""Purchase requisitions — the request that justifies an order.

Until now the purchase order was the first record in the chain, so an approver
had nothing upstream to check it against and the audit trail could prove an
order was properly approved without ever answering *why it was ordered at all*.
Every control downstream — three-way matching, maker-checker on payment,
reconciliation — verifies that money followed the order faithfully. None of
them verifies the order should have existed.

A requisition is that missing record: who asked, for what, why, and against
which budget. It carries the correlation chain that the RFQ, the quotes, the
purchase order, the receipts, the invoice and the payment all join, so the
whole story reads from the business need rather than from the commitment.

Deliberately no vendor. A requisition states a need; choosing who supplies it
is the sourcing step's decision, and naming a vendor here would let the
requester pre-select the winner before anyone has quoted.
"""
from sqlalchemy import (
    Column, String, Date, Numeric, ForeignKey, DateTime, Integer,
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, UTC_NOW
from app.core.enums import RequisitionState, Currency


class PurchaseRequisition(BaseModel):
    __tablename__ = "purchase_requisitions"

    #: Which configured state machine governs this record.
    WORKFLOW_TYPE = "requisition"

    #: How this module names itself in the audit trail and correlation chain.
    OBJECT_TYPE = "requisition"

    #: How this record names itself in a correlation chain.
    REFERENCE_FIELD = "requisition_number"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    requisition_number = Column(String(100), nullable=False, index=True)

    #: What the requester needs and why they need it. `justification` is the
    #: field an approver is actually deciding on, and the one an auditor reads
    #: first, so it is required rather than an optional note.
    title = Column(String(255), nullable=False)
    justification = Column(String, nullable=False)

    #: Free text today. A budgets module would make this a foreign key; until
    #: then recording the code the requester charged it to is still worth more
    #: than recording nothing, because it is what a reviewer checks against.
    budget_code = Column(String(100), nullable=True)
    department = Column(String(100), nullable=True)

    requested_date = Column(Date, nullable=False)
    needed_by = Column(Date, nullable=True)

    currency = Column(SQLEnum(Currency), default=Currency.PKR)
    #: The estimate the approval was given against. A later purchase order may
    #: not exceed it without the requisition being re-approved — otherwise the
    #: approval covers a number nobody agreed to.
    estimated_amount = Column(Numeric(15, 2), nullable=False)

    #: This record starts the transaction story; everything downstream inherits
    #: it (Build Book: universal correlation IDs).
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    current_state = Column(SQLEnum(RequisitionState), default=RequisitionState.DRAFT)
    #: SLA timer start, UTC for the same reason as Invoice.state_entered_at.
    state_entered_at = Column(DateTime, server_default=UTC_NOW, nullable=True)

    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    tenant = relationship("Tenant", backref="purchase_requisitions")
    lines = relationship(
        "PurchaseRequisitionLine",
        back_populates="requisition",
        cascade="all, delete-orphan",
        order_by="PurchaseRequisitionLine.line_number",
    )


class PurchaseRequisitionLine(BaseModel):
    """One requested item.

    Carries an *estimated* unit price, not a price: nobody has quoted yet. The
    distinction matters when the quotes come in — comparing what was approved
    against what was actually offered is how an approver finds out the estimate
    was optimistic before committing to it.
    """
    __tablename__ = "purchase_requisition_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    requisition_id = Column(
        UUID(as_uuid=True),
        ForeignKey("purchase_requisitions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    line_number = Column(Integer, nullable=False)
    description = Column(String, nullable=False)
    product_code = Column(String(100), nullable=True)

    quantity = Column(Numeric(15, 3), nullable=False)
    estimated_unit_price = Column(Numeric(15, 2), nullable=False)
    estimated_amount = Column(Numeric(15, 2), nullable=False)

    requisition = relationship("PurchaseRequisition", back_populates="lines")
