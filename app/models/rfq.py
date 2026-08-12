"""Requests for quotation, and the quotes that answer them.

Sourcing is where a need becomes a choice of supplier, and it is the step most
worth recording precisely: the decision is discretionary, it happens before any
money is committed, and by the time an invoice arrives it is far too late to
ask why this vendor was picked.

Two structural choices carry most of the control:

  * **Quotes are locked when the RFQ closes.** Until then vendors may revise;
    afterwards nobody can, including the buyer. A quote that can be edited
    once the field is known is not a quote, it is a formality — and back-dating
    a losing bid downwards is the cheapest way to make a rigged award look
    competitive.
  * **The award records why.** Picking anything other than the cheapest
    compliant quote is legitimate and common, and it is also the single most
    examined decision in procurement, so the reason is stored on the award
    rather than left in somebody's inbox.

Vendors do not log in. A buyer enters what each vendor quoted, which is how
this works in practice for the size of organisation this serves — so the
record says who captured it and when, and the audit trail carries the rest.
"""
from sqlalchemy import (
    Column, String, Date, Numeric, ForeignKey, DateTime, Integer, Boolean,
    UniqueConstraint, Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, UTC_NOW
from app.core.enums import RFQState, QuoteState, Currency


class RFQ(BaseModel):
    __tablename__ = "rfqs"

    WORKFLOW_TYPE = "rfq"
    OBJECT_TYPE = "rfq"

    #: How this record names itself in a correlation chain.
    REFERENCE_FIELD = "rfq_number"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    rfq_number = Column(String(100), nullable=False, index=True)
    title = Column(String(255), nullable=False)

    #: The approved need this is sourcing. Required: an RFQ with no requisition
    #: behind it is a buyer approaching the market on their own authority.
    requisition_id = Column(
        UUID(as_uuid=True), ForeignKey("purchase_requisitions.id"),
        nullable=False, index=True,
    )

    issued_date = Column(Date, nullable=True)
    #: When quoting closes. Past this the field is known, so quotes lock.
    closes_at = Column(DateTime, nullable=True)

    currency = Column(SQLEnum(Currency), default=Currency.PKR)

    #: Inherited from the requisition, so the sourcing step appears in the same
    #: story as the need that prompted it.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    current_state = Column(SQLEnum(RFQState), default=RFQState.DRAFT)
    state_entered_at = Column(DateTime, server_default=UTC_NOW, nullable=True)

    #: Set at award. Kept here as well as on the quote so "which vendor won
    #: this RFQ" is answerable without scanning every quote.
    awarded_quote_id = Column(UUID(as_uuid=True), nullable=True)
    awarded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    awarded_at = Column(DateTime, nullable=True)
    #: Why this quote and not the cheapest. Required by the service whenever
    #: the award is not the lowest compliant quote.
    award_justification = Column(String, nullable=True)

    cancellation_reason = Column(String, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    tenant = relationship("Tenant", backref="rfqs")
    requisition = relationship("PurchaseRequisition", backref="rfqs")
    invited_vendors = relationship(
        "RFQVendor", back_populates="rfq", cascade="all, delete-orphan",
    )
    quotes = relationship(
        "Quote", back_populates="rfq", cascade="all, delete-orphan",
        order_by="Quote.created_at",
    )


class RFQVendor(BaseModel):
    """A vendor invited to quote.

    Recorded separately from the quotes so that a vendor who was invited and
    did *not* respond is still visible. Who was asked is as much a part of the
    decision as who answered — an award is only competitive if the invitation
    list was.
    """
    __tablename__ = "rfq_vendors"

    #: Declared on the model as well as in the migration: dev and test
    #: databases are built with create_all, so a constraint living only in
    #: Alembic is absent exactly where it would first be exercised. Inviting
    #: the same vendor twice would inflate the apparent size of the field.
    __table_args__ = (
        UniqueConstraint("rfq_id", "vendor_id", name="uq_rfq_vendors_rfq_vendor"),
    )

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    rfq_id = Column(
        UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True,
    )
    vendor_name = Column(String(255), nullable=False)
    invited_at = Column(DateTime, server_default=UTC_NOW, nullable=False)

    rfq = relationship("RFQ", back_populates="invited_vendors")
    vendor = relationship("Vendor")


class Quote(BaseModel):
    """What one vendor offered.

    `is_compliant` is the buyer's judgement that the quote actually meets the
    requirement — a cheaper bid for the wrong specification is not the lowest
    quote, it is a different quote. Marking it explicitly keeps "cheapest" from
    silently meaning "cheapest thing anyone typed in".
    """
    __tablename__ = "quotes"

    OBJECT_TYPE = "quote"

    #: A quote has no number of its own — its identity is which vendor gave
    #: it, which is also what a reader of the chain wants to see.
    REFERENCE_FIELD = "vendor_name"

    #: One quote per vendor per tender. A revision withdraws the original
    #: rather than silently replacing it, so both stay visible — which is the
    #: whole point of locking quotes at close.
    __table_args__ = (
        UniqueConstraint("rfq_id", "vendor_id", name="uq_quotes_rfq_vendor"),
    )

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    rfq_id = Column(
        UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True,
    )
    vendor_name = Column(String(255), nullable=False)

    quote_reference = Column(String(100), nullable=True)
    quote_date = Column(Date, nullable=True)
    valid_until = Column(Date, nullable=True)

    currency = Column(SQLEnum(Currency), default=Currency.PKR)
    total_amount = Column(Numeric(15, 2), nullable=False)
    lead_time_days = Column(Integer, nullable=True)
    payment_terms = Column(String(255), nullable=True)
    notes = Column(String, nullable=True)

    #: Does it meet the requirement? False takes it out of the "cheapest"
    #: comparison, and the reason is recorded rather than assumed.
    is_compliant = Column(Boolean, nullable=False, default=True)
    non_compliance_reason = Column(String, nullable=True)

    current_state = Column(SQLEnum(QuoteState), default=QuoteState.RECEIVED)
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    #: Who typed it in, since the vendor did not.
    captured_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    rfq = relationship("RFQ", back_populates="quotes")
    vendor = relationship("Vendor")
    lines = relationship(
        "QuoteLine", back_populates="quote", cascade="all, delete-orphan",
        order_by="QuoteLine.line_number",
    )


class QuoteLine(BaseModel):
    """One priced item within a quote.

    Held per line so a comparison can show *where* one vendor is cheaper, which
    is what tells a reviewer whether a low total is a genuine saving or a
    missing item.
    """
    __tablename__ = "quote_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quote_id = Column(
        UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    line_number = Column(Integer, nullable=False)
    description = Column(String, nullable=False)
    product_code = Column(String(100), nullable=True)

    quantity = Column(Numeric(15, 3), nullable=False)
    unit_price = Column(Numeric(15, 2), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)

    quote = relationship("Quote", back_populates="lines")
