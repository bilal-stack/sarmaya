"""Payments — where money actually leaves.

The system never moves money. A payment records the decision to settle a set of
approved invoices and produces an instruction file a treasury user uploads to
their own banking portal. Nothing here talks to a bank.

That makes the record itself the control. Everything upstream — the approval
matrix, segregation of duties, three-way matching — exists to protect this
step, which until now was a single state flip performed by one person.

On correlation: a payment run may settle invoices belonging to several
different chains, so it cannot simply inherit one. It carries its own
correlation_id for the run's own story (prepared, released, exported), and each
settled invoice receives an audit entry on *its* chain naming the payment. The
run appears in every story it touches without pretending to belong to one.
"""
from sqlalchemy import (
    Column, String, Date, Numeric, ForeignKey, DateTime, Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, UTC_NOW
from app.core.enums import PaymentState, Currency


class Payment(BaseModel):
    __tablename__ = "payments"

    WORKFLOW_TYPE = "payment"
    OBJECT_TYPE = "payment"

    #: How this record names itself in a correlation chain.
    REFERENCE_FIELD = "payment_number"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    payment_number = Column(String(100), nullable=False, index=True)
    payment_date = Column(Date, nullable=False)
    #: bank_transfer today; the column exists so cheque or card runs can be
    #: distinguished without a migration when they are added.
    method = Column(String(50), nullable=False, default="bank_transfer")
    reference = Column(String(255), nullable=True)
    notes = Column(String, nullable=True)

    currency = Column(SQLEnum(Currency), default=Currency.PKR)
    total_amount = Column(Numeric(15, 2), nullable=False, default=0)

    #: The run's own chain. Settled invoices keep theirs; see the module note.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    current_state = Column(SQLEnum(PaymentState), default=PaymentState.DRAFT)
    state_entered_at = Column(DateTime, server_default=UTC_NOW, nullable=True)

    #: Maker and checker, recorded separately because the whole control is that
    #: they are different people.
    prepared_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    released_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    released_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)

    #: SHA-256 of the exported bank file, so what was handed to the bank is
    #: evidenced rather than asserted.
    bank_file_hash = Column(String(64), nullable=True)
    bank_file_generated_at = Column(DateTime, nullable=True)

    tenant = relationship("Tenant", backref="payments")
    lines = relationship(
        "PaymentLine",
        back_populates="payment",
        cascade="all, delete-orphan",
        order_by="PaymentLine.line_number",
    )


class PaymentLine(BaseModel):
    """One invoice settled by this run.

    Amount is held per line rather than only on the header so a partial
    settlement is representable, and so the run's total can never disagree with
    the sum of what it actually pays.
    """
    __tablename__ = "payment_lines"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    payment_id = Column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    invoice_id = Column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True,
    )

    line_number = Column(Numeric(6, 0), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)

    #: Copied at preparation time. Bank details can change after a run is
    #: released, and the file that went to the bank must stay reconstructable
    #: from what was true when it was built.
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255), nullable=False)
    bank_account_name = Column(String(255), nullable=True)
    bank_account_number = Column(String(100), nullable=True)
    bank_name = Column(String(255), nullable=True)
    iban = Column(String(50), nullable=True)
    swift_code = Column(String(20), nullable=True)

    payment = relationship("Payment", back_populates="lines")
    invoice = relationship("Invoice", backref="payment_lines")
