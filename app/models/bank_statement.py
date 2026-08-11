"""Bank statements — what actually happened, as told by the bank.

Everything else in the system records what the company *intended*: what it
ordered, approved, and instructed. A statement is the only record of what the
bank actually did, and reconciliation is where the two are compared.

That comparison is worth more than confirming payments cleared. It answers the
question no internal record can: **did money leave that nobody instructed?** An
unmatched debit on the statement is the single strongest fraud signal in an AP
system, because it cannot be produced by any mistake inside the workflow.

A statement is imported, never edited. Lines carry the bank's own reference and
a hash of the source file, so what was reconciled against can be shown to be
what the bank sent.
"""
from sqlalchemy import (
    Column, String, Date, Numeric, ForeignKey, DateTime, Integer, Boolean,
    Index, UniqueConstraint, text, Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel
from app.core.enums import Currency


class BankStatement(BaseModel):
    """One imported statement file."""
    __tablename__ = "bank_statements"

    OBJECT_TYPE = "bank_statement"

    #: Declared on the model, not only in the migration: the dev and test
    #: databases are built with create_all, so a constraint that lives only in
    #: Alembic is absent exactly where it would first be exercised.
    __table_args__ = (
        # Scoped to the tenant rather than global: two tenants banking with the
        # same institution can legitimately hold byte-identical files.
        UniqueConstraint("tenant_id", "file_hash", name="uq_bank_statements_tenant_file"),
    )

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    statement_reference = Column(String(100), nullable=False, index=True)
    account_identifier = Column(String(100), nullable=True)
    #: camt053 | mt940 | csv — recorded so a reader knows how the lines were
    #: derived, and so a re-import of the same format is comparable.
    source_format = Column(String(20), nullable=False)

    statement_date = Column(Date, nullable=True)
    opening_balance = Column(Numeric(15, 2), nullable=True)
    closing_balance = Column(Numeric(15, 2), nullable=True)
    currency = Column(SQLEnum(Currency), nullable=True)

    #: SHA-256 of the uploaded file. Two imports of the same statement are
    #: detectable, which matters because importing twice would double every
    #: line and make the reconciliation lie.
    file_hash = Column(String(64), nullable=False, index=True)
    original_filename = Column(String(255), nullable=True)

    imported_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    tenant = relationship("Tenant", backref="bank_statements")
    lines = relationship(
        "BankStatementLine",
        back_populates="statement",
        cascade="all, delete-orphan",
        order_by="BankStatementLine.line_number",
    )


class BankStatementLine(BaseModel):
    """One transaction on the statement.

    Debits are stored as positive amounts with `is_debit` set, rather than as
    negative numbers: the formats disagree about sign conventions, and deciding
    once at import beats every reader guessing.
    """
    __tablename__ = "bank_statement_lines"

    #: One payment, one bank debit. A second line claiming the same payment
    #: means the bank debited twice for one instruction — a duplicate payment,
    #: and the reconciliation must not be able to paper over it by matching
    #: both. The service refuses it too; this is the guarantee that holds when
    #: two reconcilers act at once.
    __table_args__ = (
        Index(
            "uq_bank_statement_lines_matched_payment",
            "matched_payment_id",
            unique=True,
            postgresql_where=text("matched_payment_id IS NOT NULL"),
        ),
    )

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    bank_statement_id = Column(
        UUID(as_uuid=True), ForeignKey("bank_statements.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    line_number = Column(Integer, nullable=False)
    value_date = Column(Date, nullable=True)
    booking_date = Column(Date, nullable=True)

    amount = Column(Numeric(15, 2), nullable=False)
    is_debit = Column(Boolean, nullable=False, default=True)
    currency = Column(SQLEnum(Currency), nullable=True)

    #: What the bank tells us about the line. `description` is the free text a
    #: human reads; `bank_reference` is what the bank uses to identify it.
    description = Column(String, nullable=True)
    counterparty = Column(String(255), nullable=True)
    bank_reference = Column(String(255), nullable=True, index=True)

    #: The payment this line settles, once a human has confirmed it. Null means
    #: unreconciled — and an unmatched debit is the line worth investigating.
    matched_payment_id = Column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True, index=True,
    )
    matched_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    matched_at = Column(DateTime, nullable=True)

    statement = relationship("BankStatement", back_populates="lines")
    matched_payment = relationship("Payment", backref="statement_lines")
